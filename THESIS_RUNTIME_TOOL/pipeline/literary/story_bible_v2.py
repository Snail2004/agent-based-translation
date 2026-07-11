from __future__ import annotations

"""Zero-API scaffold for the B4 Story Bible v2 contract.

This module deliberately does not reuse the pilot M3 resolver.  The pilot makes
book-specific-looking identity decisions from surface keys; B4 v2 only prepares
evidence atoms and validates model verdicts.  The actual API execution is kept
behind a later gate.  This file therefore makes the data contract, estimate,
checkpoint contract, and prompt rendering reviewable before any model call.
"""

import copy
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from pipeline.agents.llm_client import LLMClient, LLMResult, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.builder_pilot import (
    M2_CHECKPOINT_SCHEMA_VERSION,
    PHASE_LABELS,
    RESPONSE_FORMAT_JSON,
    _checkpoint_path,
    _checkpoint_prompt_hashes,
    _load_m1_report,
    _m1_checkpoint_chain_for_m2,
    _surface_key,
    load_system_prompt_for_chapter,
    select_chapters,
)
from pipeline.literary.checkpoint import (
    CheckpointLock,
    artifact_manifest,
    build_checkpoint,
    canonical_hash,
    chapter_source_hash,
    config_hash,
    read_checkpoint,
    validate_checkpoint,
    write_checkpoint_atomic,
)


IDENTITY_PARTITION_VERSION = "literary_identity_partition_v1"
PHASE_SEGMENT_VERSION = "literary_phase_segment_v2"
M3_V2_CHECKPOINT_SCHEMA_VERSION = "literary_m3_v2_checkpoint_v2"
M3_V2_STAGE = "m3_v2"
IDENTITY_REFERENT_KINDS = {
    "person",
    "place",
    "group_reference",
    "literary_allusion",
    "unknown",
}
IDENTITY_GROUP_STATUSES = {"resolved", "uncertain", "quarantine"}
PREDICATE_TAXONOMY_VERSION = "literary_predicate_taxonomy_v1"
PREDICATE_CODES = {
    "parent_of",
    "child_of",
    "spouse_of",
    "sibling_of",
    "daughter_in_law_of",
    "son_in_law_of",
    "father_in_law_of",
    "mother_in_law_of",
    "grandparent_of",
    "grandchild_of",
    "cousin_of",
    "servant_of",
    "master_of",
    "landlord_of",
    "tenant_of",
    "guest_of",
    "neighbor_of",
    "guardian_of",
    "ward_of",
    "other",
}


RequestLLM = Callable[[list[dict[str, str]], dict[str, Any]], LLMResult]
M3_V2_MAX_TECHNICAL_RETRY_RATE = 0.10


def _m3_v2_checkpoint_path(out_dir: Path, chapter_id: str) -> Path:
    return Path(out_dir) / "checkpoints" / M3_V2_STAGE / f"{chapter_id}.json"


def _m3_v2_prompt_hashes(design_doc: Path, chapter_id: str) -> dict[str, str]:
    return {
        version: canonical_hash(load_system_prompt_for_chapter(design_doc, version, chapter_id))
        for version in [IDENTITY_PARTITION_VERSION, PHASE_SEGMENT_VERSION]
    }


def _m3_v2_config_hash(config: LLMConfig) -> str:
    """Hash only values that can alter an M3 v2 model response or its shape."""

    return config_hash(
        {
            "stage": M3_V2_STAGE,
            "model": config.model,
            "temperature": config.temperature,
            "seed": config.seed,
            "reasoning_effort": config.reasoning_effort,
            "verbosity": config.verbosity,
            "response_format": RESPONSE_FORMAT_JSON,
            "max_output_tokens": config.max_output_tokens,
            "prompt_token_cap": config.prompt_token_cap,
            "predicate_taxonomy_version": PREDICATE_TAXONOMY_VERSION,
            "identity_prompt_version": IDENTITY_PARTITION_VERSION,
            "phase_prompt_version": PHASE_SEGMENT_VERSION,
        }
    )


def _require_full_prefix(document: dict[str, Any], selected: list[dict[str, Any]]) -> None:
    document_chapters = document.get("chapters") or []
    if not selected or not document_chapters:
        return
    actual = [str(item.get("chapter_id") or "") for item in selected]
    expected = [
        str(item.get("chapter_id") or "")
        for item in document_chapters[: len(selected)]
    ]
    if actual != expected:
        raise ValueError(
            "M3 v2 requires an absolute chapter prefix from the document start; "
            f"expected {expected}, got {actual}"
        )


def _m2_checkpoint_chain_for_m3(
    *,
    document: dict[str, Any],
    selected: list[dict[str, Any]],
    m2_dir: Path,
    design_doc: Path,
    m1_checkpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate M2's full ancestor chain against its matching M1 checkpoint."""

    parent_hash: str | None = None
    expected_config_hash: str | None = None
    results: list[dict[str, Any]] = []
    for index, (chapter, m1_checkpoint) in enumerate(zip(selected, m1_checkpoints, strict=True)):
        chapter_id = str(chapter["chapter_id"])
        path = _checkpoint_path(m2_dir, "m2", chapter_id)
        if not path.is_file():
            raise ValueError(f"M3 v2 requires M2 as-of checkpoint for {chapter_id}: {path}")
        checkpoint = read_checkpoint(path)
        checkpoint_config_hash = str(checkpoint.get("config_hash") or "")
        if not checkpoint_config_hash:
            raise ValueError(f"Invalid M2 as-of checkpoint {chapter_id}: ['config_hash']")
        if expected_config_hash is None:
            expected_config_hash = checkpoint_config_hash
        expected = {
            "stage": "m2",
            "chapter_id": chapter_id,
            "chapter_index": index,
            "chapter_sequence_prefix": [
                str(item["chapter_id"]) for item in selected[: index + 1]
            ],
            "source_hash": chapter_source_hash(chapter),
            "prompt_hashes": _checkpoint_prompt_hashes(design_doc, "m2", chapter_id),
            "schema_version": M2_CHECKPOINT_SCHEMA_VERSION,
            "parent_checkpoint_hash": parent_hash,
            "input_m1_checkpoint_hash": str(m1_checkpoint["checkpoint_hash"]),
            "config_hash": expected_config_hash,
        }
        errors = validate_checkpoint(checkpoint, root=m2_dir, expected=expected)
        if errors:
            raise ValueError(f"Invalid M2 as-of checkpoint {chapter_id}: {errors}")
        results.append(checkpoint)
        parent_hash = str(checkpoint["checkpoint_hash"])
    return results


def load_m3_v2_input_chain(
    *,
    document: dict[str, Any],
    chapters: list[str],
    m1_dir: Path,
    m2_dir: Path,
    design_doc: Path,
) -> dict[str, Any]:
    """Return only verified, as-of M1/M2 checkpoint inputs for B4 v2."""

    selected = select_chapters(document, chapters)
    _require_full_prefix(document, selected)
    m1_report = _load_m1_report(m1_dir)
    m1_checkpoints, absolute_chapters = _m1_checkpoint_chain_for_m2(
        document=document,
        selected=selected,
        m1_dir=Path(m1_dir),
        design_doc=Path(design_doc),
        m1_report=m1_report,
    )
    if len(m1_checkpoints) != len(selected):
        raise ValueError("M3 v2 refuses legacy M1 input without a full checkpoint chain")
    m2_checkpoints = _m2_checkpoint_chain_for_m3(
        document=document,
        selected=selected,
        m2_dir=Path(m2_dir),
        design_doc=Path(design_doc),
        m1_checkpoints=m1_checkpoints,
    )
    return {
        "selected": selected,
        "absolute_chapters": absolute_chapters,
        "m1_checkpoints": m1_checkpoints,
        "m2_checkpoints": m2_checkpoints,
        "m1_report": m1_report,
    }


def _manifest_paths(
    *,
    root: Path,
    checkpoints: Iterable[dict[str, Any]],
    directory: str,
) -> list[Path]:
    """Read only paths explicitly allowed by the validated checkpoint chain."""

    root = Path(root).resolve()
    paths: list[Path] = []
    seen: set[str] = set()
    prefix = f"{directory.rstrip('/')}/"
    for checkpoint in checkpoints:
        for row in checkpoint.get("artifact_manifest") or []:
            relative = str((row or {}).get("path") or "")
            if not relative.startswith(prefix):
                continue
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"M3 v2 manifest path escapes root: {relative}") from exc
            if relative not in seen:
                paths.append(candidate)
                seen.add(relative)
    return sorted(paths, key=lambda item: item.as_posix())


def _clean_parsed_payload(path: Path, *, expected_chapter: str | None = None) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    parsed = payload.get("parsed_json")
    validation = payload.get("validation") or {}
    if not isinstance(parsed, dict) or not validation.get("ok"):
        raise ValueError(f"M3 v2 requires a clean parsed artifact: {path}")
    if expected_chapter is not None and str(parsed.get("chapter_id") or "") != expected_chapter:
        raise ValueError(f"M3 v2 artifact chapter mismatch: {path}")
    return parsed


def _block_maps(document: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    by_id: dict[str, dict[str, Any]] = {}
    chapter_for_block: dict[str, str] = {}
    for chapter in document.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "")
        for block in chapter.get("blocks") or []:
            block_id = str(block.get("block_id") or "")
            if block_id:
                by_id[block_id] = block
                chapter_for_block[block_id] = chapter_id
    return by_id, chapter_for_block


def _quote_context(text: str, surface: str, *, limit: int = 360) -> str:
    """Extract a deterministic evidence window without interpreting the prose."""

    source = str(text or "")
    needle = str(surface or "").strip()
    if not needle or not source:
        return source[:limit]
    match = re.search(re.escape(needle), source, flags=re.IGNORECASE)
    if match is None:
        return source[:limit]
    start = max(0, match.start() - limit // 2)
    end = min(len(source), start + limit)
    start = max(0, end - limit)
    return source[start:end]


def _atom_id(mention_id: str, block_id: str) -> str:
    return "atom_" + re.sub(r"[^a-zA-Z0-9_]+", "_", f"{mention_id}__{block_id}").strip("_")


def build_identity_atoms_as_of(
    *,
    document: dict[str, Any],
    m1_dir: Path,
    m1_checkpoints: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one atom per validated B1 mention row and source block.

    A B1 row can legitimately cite more than one active block.  The atom contract
    is therefore `(mention_id x block_id)`, not a chapter-level surface bucket.
    Exact duplicates introduced by overlapping windows are mechanically deduped.
    """

    block_by_id, chapter_for_block = _block_maps(document)
    m1_root = Path(m1_dir).resolve()
    atoms: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicate_rows = 0
    for artifact_path in _manifest_paths(
        root=Path(m1_dir), checkpoints=m1_checkpoints, directory="lexicon"
    ):
        parsed = _clean_parsed_payload(artifact_path)
        for mention in parsed.get("character_mentions") or []:
            if not isinstance(mention, dict):
                continue
            mention_id = str(mention.get("mention_id") or "").strip()
            surface = str(mention.get("surface") or "").strip()
            if not mention_id or not surface:
                continue
            candidate_ids = [
                str(value) for value in mention.get("candidate_entity_ids") or [] if str(value)
            ]
            hint_entity_id = candidate_ids[0] if len(candidate_ids) == 1 else None
            for block_id in [str(value) for value in mention.get("block_ids") or [] if str(value)]:
                block = block_by_id.get(block_id)
                if block is None:
                    raise ValueError(
                        f"M3 v2 mention references block outside current document: {block_id}"
                    )
                key = (mention_id, block_id, surface.casefold())
                candidate = {
                    "atom_id": _atom_id(mention_id, block_id),
                    "mention_id": mention_id,
                    "block_id": block_id,
                    "chapter_id": chapter_for_block[block_id],
                    "surface": surface,
                    "quote_context": _quote_context(
                        str(block.get("clean_text") or block.get("source_text") or ""),
                        surface,
                    ),
                    "hint_entity_id": hint_entity_id,
                    "source_artifact": artifact_path.relative_to(m1_root).as_posix(),
                }
                previous = seen.get(key)
                if previous is not None:
                    duplicate_rows += 1
                    if previous["hint_entity_id"] != candidate["hint_entity_id"]:
                        raise ValueError(
                            "M3 v2 exact duplicate B1 mention has conflicting hint ids: "
                            f"{mention_id} / {block_id}"
                        )
                    continue
                seen[key] = candidate
                atoms.append(candidate)
    atoms.sort(key=lambda item: (str(item["block_id"]), str(item["mention_id"]), str(item["surface"])))
    return {"atoms": atoms, "duplicate_rows_deduped": duplicate_rows}


def _digest_payloads_as_of(
    *,
    m2_dir: Path,
    m2_checkpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for checkpoint in m2_checkpoints:
        chapter_id = str(checkpoint["chapter_id"])
        paths = _manifest_paths(root=Path(m2_dir), checkpoints=[checkpoint], directory="digest")
        if len(paths) != 1:
            raise ValueError(
                f"M3 v2 requires exactly one digest in checkpoint manifest for {chapter_id}"
            )
        payloads.append(_clean_parsed_payload(paths[0], expected_chapter=chapter_id))
    return payloads


def _provisional_groups(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Non-publishable, mechanical sizing state used only by the 0-API scaffold.

    It groups by a B1/B2 hint id when one exists and otherwise keeps an atom
    singleton.  It must never be written as a Story Bible identity decision.
    """

    groups: dict[str, list[dict[str, Any]]] = {}
    for atom in atoms:
        key = str(atom.get("hint_entity_id") or f"atom:{atom['atom_id']}")
        groups.setdefault(key, []).append(atom)
    rows: list[dict[str, Any]] = []
    for key, members in sorted(groups.items()):
        first = members[0]
        rows.append(
            {
                "entity_id": f"provisional_{_surface_key(key) or canonical_hash(key)[:12]}",
                "canonical_surface": str(first["surface"]),
                "referent_kind": "unknown",
                "member_summary": [str(item["atom_id"]) for item in members[:8]],
                "member_surface_keys": sorted(
                    {_surface_key(str(item["surface"])) for item in members}
                ),
                "hint_entity_id": key if not key.startswith("atom:") else None,
                "provisional_only": True,
            }
        )
    return rows


def _frontier_prior_groups(
    *,
    prior_atoms: list[dict[str, Any]],
    frontier_atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select only mechanically linked previous groups for prompt boundedness."""

    surface_keys = {_surface_key(str(atom["surface"])) for atom in frontier_atoms}
    hint_ids = {str(atom["hint_entity_id"]) for atom in frontier_atoms if atom.get("hint_entity_id")}
    candidates = _provisional_groups(prior_atoms)
    result: list[dict[str, Any]] = []
    for group in candidates:
        linked = bool(
            bool(set(group.get("member_surface_keys") or []) & surface_keys)
            or str(group.get("hint_entity_id") or "") in hint_ids
        )
        if linked:
            result.append(group)
    return result


def _identity_components(atoms: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Build candidate components using only exact surface/hint equality.

    These are prompt-sharding components, not identity verdicts.  The union is
    intentionally weak: it merely prevents two visibly linked atoms from being
    sent to different calls where the model could not compare them.
    """

    parent = list(range(len(atoms)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    first_by_key: dict[str, int] = {}
    for index, atom in enumerate(atoms):
        keys = [f"surface:{_surface_key(str(atom['surface']))}"]
        hint = str(atom.get("hint_entity_id") or "")
        if hint:
            keys.append(f"hint:{hint}")
        for key in keys:
            if key in first_by_key:
                union(index, first_by_key[key])
            else:
                first_by_key[key] = index
    groups: dict[int, list[dict[str, Any]]] = {}
    for index, atom in enumerate(atoms):
        groups.setdefault(find(index), []).append(atom)
    return sorted(
        [sorted(group, key=lambda item: (item["block_id"], item["atom_id"])) for group in groups.values()],
        key=lambda group: (group[0]["block_id"], group[0]["atom_id"]),
    )


def _identity_hints(digests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for digest in digests:
        rows.append(
            {
                "chapter_id": str(digest.get("chapter_id") or ""),
                "narration_frame_segments": digest.get("narration_frame_segments") or [],
                "relation_event_summary": digest.get("relation_event_summary") or [],
                "translator_relevant_facts": digest.get("translator_relevant_facts") or [],
            }
        )
    return rows


def build_identity_messages(
    *,
    design_doc: Path,
    chapter_id: str,
    scope: str,
    atoms: list[dict[str, Any]],
    prior_groups: list[dict[str, Any]],
    identity_hints: list[dict[str, Any]],
    scaffold_only: bool = True,
) -> list[dict[str, str]]:
    prompt_prior_groups = [
        {
            "entity_id": group["entity_id"],
            "canonical_surface": group["canonical_surface"],
            "referent_kind": group["referent_kind"],
            "member_summary": group["member_summary"],
        }
        for group in prior_groups
    ]
    user_payload: dict[str, Any] = {
        "scope": scope,
        "atoms": atoms,
        "prior_groups": prompt_prior_groups,
        "identity_hints": identity_hints,
    }
    if scaffold_only:
        user_payload["dry_run_note"] = (
            "This scaffold uses provisional prior groups only for token sizing. "
            "They are not identity verdicts and will not be published."
        )
    return [
        {
            "role": "system",
            "content": load_system_prompt_for_chapter(
                design_doc, IDENTITY_PARTITION_VERSION, chapter_id
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]


def _event_index_from_manifests(
    *,
    m1_dir: Path,
    m1_checkpoints: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for artifact_path in _manifest_paths(
        root=Path(m1_dir), checkpoints=m1_checkpoints, directory="narrative"
    ):
        parsed = _clean_parsed_payload(artifact_path)
        for event in parsed.get("relation_events") or []:
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id:
                continue
            row = {
                "event_id": event_id,
                "block_id": str(event.get("block_id") or ""),
                "event_type": str(event.get("event_type") or ""),
                "evidence_quote": str(event.get("evidence_quote") or ""),
                "actor": event.get("actor") or {},
                "target": event.get("target") or {},
            }
            previous = events.get(event_id)
            if previous is not None and previous != row:
                raise ValueError(f"M3 v2 relation event id collision: {event_id}")
            events[event_id] = row
    return events


def _phase_rows_as_of(
    *,
    digests: list[dict[str, Any]],
    event_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for digest in digests:
        chapter_id = str(digest.get("chapter_id") or "")
        for relation in digest.get("relation_event_summary") or []:
            if not isinstance(relation, dict):
                continue
            pair = [str(value) for value in relation.get("pair") or [] if str(value)]
            if len(pair) != 2 or pair[0] == pair[1]:
                continue
            event_ids = [str(value) for value in relation.get("event_ids") or [] if str(value)]
            missing_event_ids = [event_id for event_id in event_ids if event_id not in event_index]
            if missing_event_ids:
                raise ValueError(
                    "M3 v2 phase evidence join missing "
                    f"for {chapter_id} pair={sorted(pair)}: {sorted(missing_event_ids)}"
                )
            rows.append(
                {
                    "source_chapter_id": chapter_id,
                    "provisional_pair": sorted(pair),
                    "event_ids": event_ids,
                    "events": [event_index[event_id] for event_id in event_ids],
                    "candidate_transition": relation.get("candidate_transition"),
                    "observed_valence_hint": relation.get("observed_valence_hint"),
                }
            )
    rows.sort(key=lambda item: (item["provisional_pair"], item["source_chapter_id"]))
    return rows


def build_phase_messages(
    *,
    design_doc: Path,
    chapter_id: str,
    scope: str,
    phase_rows: list[dict[str, Any]],
    scaffold_only: bool = True,
) -> list[dict[str, str]]:
    user_payload: dict[str, Any] = {
        "scope": scope,
        "predicate_taxonomy_version": PREDICATE_TAXONOMY_VERSION,
        "pair_evidence": phase_rows,
        "response_envelope": {
            "relation_facts": "list",
            "relation_phases": "list; every phase must include its pair because this is a batch",
        },
    }
    if scaffold_only:
        user_payload["dry_run_note"] = (
            "Pair ids are pre-identity provisional ids for prompt sizing only. "
            "A real run remaps evidence to final ids after identity partition before this call."
        )
    return [
        {
            "role": "system",
            "content": load_system_prompt_for_chapter(
                design_doc, PHASE_SEGMENT_VERSION, chapter_id
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]


def _shard_for_cap(
    *,
    items: list[dict[str, Any]],
    build_messages: Callable[[list[dict[str, Any]]], list[dict[str, str]]],
    prompt_cap: int | None,
) -> list[dict[str, Any]]:
    """Greedy deterministic shards; items are already sorted by source order."""

    cap = int(prompt_cap or 10**12)
    if not items:
        messages = build_messages([])
        return [{"items": [], "messages": messages, "prompt_tokens_est": estimate_prompt_tokens(messages, RESPONSE_FORMAT_JSON)}]
    shards: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for item in items:
        candidate = [*current, item]
        candidate_messages = build_messages(candidate)
        candidate_tokens = estimate_prompt_tokens(candidate_messages, RESPONSE_FORMAT_JSON)
        if current and candidate_tokens > cap:
            current_messages = build_messages(current)
            shards.append(
                {
                    "items": current,
                    "messages": current_messages,
                    "prompt_tokens_est": estimate_prompt_tokens(current_messages, RESPONSE_FORMAT_JSON),
                }
            )
            current = [item]
            candidate_messages = build_messages(current)
            candidate_tokens = estimate_prompt_tokens(candidate_messages, RESPONSE_FORMAT_JSON)
            if candidate_tokens > cap:
                raise ValueError(
                    "M3 v2 cannot shard one item below prompt cap: "
                    f"{candidate_tokens} > {cap}"
                )
            continue
        if candidate_tokens > cap:
            raise ValueError(
                "M3 v2 cannot shard one item below prompt cap: "
                f"{candidate_tokens} > {cap}"
            )
        current = candidate
    if current:
        messages = build_messages(current)
        shards.append(
            {
                "items": current,
                "messages": messages,
                "prompt_tokens_est": estimate_prompt_tokens(messages, RESPONSE_FORMAT_JSON),
            }
        )
    return shards


def _identity_shards(
    *,
    frontier_atoms: list[dict[str, Any]],
    prior_atoms: list[dict[str, Any]],
    design_doc: Path,
    chapter_id: str,
    scope: str,
    identity_hints: list[dict[str, Any]],
    prompt_cap: int | None,
) -> list[dict[str, Any]]:
    components = _identity_components(frontier_atoms)

    def build_for_components(component_batch: list[dict[str, Any]]) -> list[dict[str, str]]:
        atoms = [atom for component in component_batch for atom in component["atoms"]]
        prior_groups = _frontier_prior_groups(prior_atoms=prior_atoms, frontier_atoms=atoms)
        return build_identity_messages(
            design_doc=design_doc,
            chapter_id=chapter_id,
            scope=scope,
            atoms=atoms,
            prior_groups=prior_groups,
            identity_hints=identity_hints,
        )

    component_rows = [
        {
            "component_id": f"component_{index:04d}",
            "atoms": component,
        }
        for index, component in enumerate(components, start=1)
    ]
    shards = _shard_for_cap(
        items=component_rows,
        build_messages=build_for_components,
        prompt_cap=prompt_cap,
    )
    for shard in shards:
        atoms = [atom for component in shard["items"] for atom in component["atoms"]]
        shard["items"] = atoms
        shard["component_count"] = len(component_rows)
        shard["prior_groups"] = _frontier_prior_groups(
            prior_atoms=prior_atoms,
            frontier_atoms=atoms,
        )
    return shards


def _runtime_prior_groups(
    *,
    state: dict[str, Any],
    frontier_atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose only already-published groups mechanically linked to this frontier."""

    frontier_surfaces = {
        _surface_key(str(atom.get("surface") or ""))
        for atom in frontier_atoms
        if str(atom.get("surface") or "")
    }
    frontier_hints = {
        str(atom.get("hint_entity_id") or "")
        for atom in frontier_atoms
        if str(atom.get("hint_entity_id") or "")
    }
    groups: list[dict[str, Any]] = []
    for entity in state.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        entity_id = str(entity.get("entity_id") or "")
        canonical = str(entity.get("canonical") or "")
        if not entity_id or not canonical:
            continue
        aliases = [canonical]
        for alias in entity.get("aliases") or []:
            if isinstance(alias, dict) and str(alias.get("surface") or ""):
                aliases.append(str(alias["surface"]))
        alias_keys = {_surface_key(surface) for surface in aliases if surface}
        if entity_id not in frontier_hints and not (alias_keys & frontier_surfaces):
            continue
        groups.append(
            {
                "entity_id": entity_id,
                "canonical_surface": canonical,
                "referent_kind": str(entity.get("referent_kind") or "unknown"),
                "member_summary": [
                    str(atom_id) for atom_id in (entity.get("member_atom_ids") or [])[:12]
                ],
            }
        )
    return sorted(groups, key=lambda item: str(item["entity_id"]))


def _runtime_identity_shards(
    *,
    frontier_atoms: list[dict[str, Any]],
    state: dict[str, Any],
    design_doc: Path,
    chapter_id: str,
    scope: str,
    identity_hints: list[dict[str, Any]],
    prompt_cap: int | None,
) -> list[dict[str, Any]]:
    """Build the post-scaffold identity request plan with no dry-run-only fields."""

    components = _identity_components(frontier_atoms)
    component_rows = [
        {"component_id": f"component_{index:04d}", "atoms": component}
        for index, component in enumerate(components, start=1)
    ]

    def build_for_components(component_batch: list[dict[str, Any]]) -> list[dict[str, str]]:
        atoms = [atom for component in component_batch for atom in component["atoms"]]
        return build_identity_messages(
            design_doc=design_doc,
            chapter_id=chapter_id,
            scope=scope,
            atoms=atoms,
            prior_groups=_runtime_prior_groups(state=state, frontier_atoms=atoms),
            identity_hints=identity_hints,
            scaffold_only=False,
        )

    shards = _shard_for_cap(
        items=component_rows,
        build_messages=build_for_components,
        prompt_cap=prompt_cap,
    )
    for shard in shards:
        shard["items"] = [
            atom for component in shard["items"] for atom in component["atoms"]
        ]
    return shards


def _phase_pair_batches(
    *,
    phase_rows: list[dict[str, Any]],
    chapter_id: str,
) -> list[dict[str, Any]]:
    """Replay complete history only for pairs that receive current-scope evidence."""

    affected = {
        tuple(row["provisional_pair"])
        for row in phase_rows
        if row["source_chapter_id"] == chapter_id
    }
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in phase_rows:
        key = tuple(row["provisional_pair"])
        if key in affected:
            by_pair.setdefault(key, []).append(row)
    return [
        {
            "provisional_pair": list(pair),
            "history": history,
        }
        for pair, history in sorted(by_pair.items())
    ]


def _merge_mapped_phase_batches(
    *,
    phase_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Merge model-resolved provisional histories by final pair without interpretation."""

    grouped: dict[tuple[str, str], list[tuple[str, int, int, dict[str, Any]]]] = {}
    input_event_ids: set[str] = set()
    errors: list[str] = []

    for batch_index, row in enumerate(phase_rows):
        pair = tuple(sorted(str(value) for value in row.get("pair") or [] if str(value)))
        if len(pair) != 2 or pair[0] == pair[1]:
            errors.append(f"invalid_final_pair:{list(pair)}")
            continue
        history = row.get("history")
        if not isinstance(history, list) or not history:
            errors.append(f"empty_history:{list(pair)}")
            continue
        for history_index, item in enumerate(history):
            if not isinstance(item, dict):
                errors.append(f"invalid_history:{list(pair)}")
                continue
            event_ids = [str(value) for value in item.get("event_ids") or [] if str(value)]
            events = item.get("events")
            if not event_ids or not isinstance(events, list) or not events:
                errors.append(f"empty_events:{list(pair)}")
                continue
            joined_event_ids = [str(event.get("event_id") or "") for event in events if isinstance(event, dict)]
            if set(event_ids) != set(joined_event_ids) or len(event_ids) != len(joined_event_ids):
                errors.append(f"event_join_mismatch:{list(pair)}")
                continue
            input_event_ids.update(event_ids)
            grouped.setdefault(pair, []).append(
                (
                    str(item.get("source_chapter_id") or ""),
                    batch_index,
                    history_index,
                    copy.deepcopy(item),
                )
            )

    if errors:
        raise M3V2TechnicalGateError("phase_input_wiring_error", sorted(set(errors)))

    merged: list[dict[str, Any]] = []
    for pair, rows in sorted(grouped.items()):
        history = [item for _chapter, _batch, _history, item in sorted(rows)]
        if not history or not any(item.get("events") for item in history):
            raise M3V2TechnicalGateError(
                "phase_input_wiring_error", [f"empty_final_pair:{list(pair)}"]
            )
        merged.append({"pair": list(pair), "history": history})

    output_event_ids = {
        str(event.get("event_id") or "")
        for batch in merged
        for history in batch["history"]
        for event in history.get("events") or []
        if isinstance(event, dict) and str(event.get("event_id") or "")
    }
    if input_event_ids != output_event_ids:
        raise M3V2TechnicalGateError(
            "phase_input_wiring_error", ["event_id_set_not_preserved"]
        )

    return merged, {
        "provisional_pair_batches": len(phase_rows),
        "final_pairs_sent": len(merged),
        "collapsed_pair_batches": len(phase_rows) - len(merged),
        "history_rows_sent": sum(len(batch["history"]) for batch in merged),
        "events_sent": sum(
            len(history.get("events") or [])
            for batch in merged
            for history in batch["history"]
        ),
    }


def _runtime_phase_shards(
    *,
    phase_batches: list[dict[str, Any]],
    design_doc: Path,
    chapter_id: str,
    scope: str,
    prompt_cap: int | None,
) -> list[dict[str, Any]]:
    """Build phase requests after all references point to final identity ids."""

    return _shard_for_cap(
        items=phase_batches,
        build_messages=lambda shard: build_phase_messages(
            design_doc=design_doc,
            chapter_id=chapter_id,
            scope=scope,
            phase_rows=shard,
            scaffold_only=False,
        ),
        prompt_cap=prompt_cap,
    )


def _scope_payloads(
    *,
    document: dict[str, Any],
    chain: dict[str, Any],
    m1_dir: Path,
    m2_dir: Path,
    design_doc: Path,
    config: LLMConfig,
) -> list[dict[str, Any]]:
    atom_result = build_identity_atoms_as_of(
        document=document,
        m1_dir=m1_dir,
        m1_checkpoints=chain["m1_checkpoints"],
    )
    all_atoms = atom_result["atoms"]
    digests = _digest_payloads_as_of(m2_dir=m2_dir, m2_checkpoints=chain["m2_checkpoints"])
    event_index = _event_index_from_manifests(m1_dir=m1_dir, m1_checkpoints=chain["m1_checkpoints"])
    phase_rows = _phase_rows_as_of(digests=digests, event_index=event_index)
    scopes: list[dict[str, Any]] = []
    selected = chain["selected"]
    for index, chapter in enumerate(selected):
        chapter_id = str(chapter["chapter_id"])
        scope = f"M3_asof_{chapter_id}"
        as_of_chapters = [str(item["chapter_id"]) for item in selected[: index + 1]]
        frontier_atoms = [item for item in all_atoms if item["chapter_id"] == chapter_id]
        prior_atoms = [item for item in all_atoms if item["chapter_id"] in set(as_of_chapters[:-1])]
        # Frontier-incremental: prior digest facts were already adjudicated into
        # the prior-group state.  Re-sending every historical digest to every
        # scope both leaks attention and defeats the bounded frontier contract.
        hints = _identity_hints([digests[index]])
        identity_shards = _identity_shards(
            frontier_atoms=frontier_atoms,
            prior_atoms=prior_atoms,
            design_doc=design_doc,
            chapter_id=chapter_id,
            scope=scope,
            identity_hints=hints,
            prompt_cap=config.prompt_token_cap,
        )
        scope_phase_rows = _phase_pair_batches(
            phase_rows=[
                item for item in phase_rows if item["source_chapter_id"] in set(as_of_chapters)
            ],
            chapter_id=chapter_id,
        )
        phase_shards = _shard_for_cap(
            items=scope_phase_rows,
            build_messages=lambda shard, c=chapter_id, s=scope: build_phase_messages(
                design_doc=design_doc,
                chapter_id=c,
                scope=s,
                phase_rows=shard,
            ),
            prompt_cap=config.prompt_token_cap,
        )
        scopes.append(
            {
                "scope": scope,
                "chapter_id": chapter_id,
                "as_of_chapters": as_of_chapters,
                "m1_checkpoint_hash": str(chain["m1_checkpoints"][index]["checkpoint_hash"]),
                "m2_checkpoint_hash": str(chain["m2_checkpoints"][index]["checkpoint_hash"]),
                "atoms_total_as_of": sum(1 for item in all_atoms if item["chapter_id"] in set(as_of_chapters)),
                "frontier_atoms": frontier_atoms,
                "identity_hints": hints,
                "identity_shards": identity_shards,
                "phase_rows": scope_phase_rows,
                "phase_shards": phase_shards,
            }
        )
    return scopes


def estimate_m3_v2(
    document: dict[str, Any],
    chapters: list[str],
    *,
    design_doc: Path,
    config: LLMConfig,
    m1_dir: Path,
    m2_dir: Path,
) -> dict[str, Any]:
    """Render-free, 0-API estimate for B4 v2's logical identity and phase stages."""

    chain = load_m3_v2_input_chain(
        document=document,
        chapters=chapters,
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        design_doc=design_doc,
    )
    scopes = _scope_payloads(
        document=document,
        chain=chain,
        m1_dir=Path(m1_dir),
        m2_dir=Path(m2_dir),
        design_doc=Path(design_doc),
        config=config,
    )
    calls: list[dict[str, Any]] = []
    for scope in scopes:
        for mode, shards in [
            (IDENTITY_PARTITION_VERSION, scope["identity_shards"]),
            (PHASE_SEGMENT_VERSION, scope["phase_shards"]),
        ]:
            for shard_index, shard in enumerate(shards, start=1):
                calls.append(
                    {
                        "scope": scope["scope"],
                        "chapter_id": scope["chapter_id"],
                        "mode": mode,
                        "shard_index": shard_index,
                        "shard_count": len(shards),
                        "items": len(shard["items"]),
                        "prompt_tokens_est": int(shard["prompt_tokens_est"]),
                        "max_output_tokens": config.max_output_tokens,
                    }
                )
    total_prompt_tokens = sum(int(item["prompt_tokens_est"]) for item in calls)
    max_prompt_tokens = max((int(item["prompt_tokens_est"]) for item in calls), default=0)
    total_upper = total_prompt_tokens + len(calls) * config.max_output_tokens
    pricing = config.pricing
    cost_cap = round(
        (total_prompt_tokens / 1_000_000) * pricing["input"]
        + (len(calls) * config.max_output_tokens / 1_000_000) * pricing["output"],
        12,
    )
    return {
        "phase": "L2A-M4d",
        "milestone": "M3_v2_scaffold",
        "zero_api": True,
        "prompt_source": str(design_doc),
        "model": config.model,
        "chapters_selected": [str(item["chapter_id"]) for item in chain["selected"]],
        "modes": [IDENTITY_PARTITION_VERSION, PHASE_SEGMENT_VERSION],
        "logical_stages_per_scope": 2,
        "physical_calls": len(calls),
        "call_estimates": calls,
        "scope_summary": [
            {
                "scope": scope["scope"],
                "atoms_total_as_of": scope["atoms_total_as_of"],
                "frontier_atoms": len(scope["frontier_atoms"]),
                "prior_groups_for_frontier": len(
                    {
                        str(group["entity_id"])
                        for shard in scope["identity_shards"]
                        for group in shard.get("prior_groups") or []
                    }
                ),
                "prior_group_injections": sum(
                    len(shard.get("prior_groups") or []) for shard in scope["identity_shards"]
                ),
                "phase_rows_as_of": len(scope["phase_rows"]),
                "identity_shards": len(scope["identity_shards"]),
                "phase_shards": len(scope["phase_shards"]),
                "m1_checkpoint_hash": scope["m1_checkpoint_hash"],
                "m2_checkpoint_hash": scope["m2_checkpoint_hash"],
            }
            for scope in scopes
        ],
        "prompt_tokens_est": total_prompt_tokens,
        "max_prompt_tokens_est": max_prompt_tokens,
        "max_output_tokens_per_call": config.max_output_tokens,
        "total_tokens_upper_bound": total_upper,
        "prompt_token_cap": config.prompt_token_cap,
        "cost_cap_usd": cost_cap,
        "token_growth_halt": max_prompt_tokens > int(config.prompt_token_cap or 10**12),
        "scaffold_only": True,
        "note": (
            "Dry-run phase prompts use B1/B3 provisional hints only to size the second "
            "logical stage. They are not B4 decisions and no Story Bible/checkpoint is published."
        ),
    }


def build_m3_v2_checkpoint(
    *,
    out_dir: Path,
    chapter: dict[str, Any],
    chapter_index: int,
    chapter_sequence_prefix: list[str],
    design_doc: Path,
    config: LLMConfig,
    input_m1_checkpoint_hash: str,
    input_m2_checkpoint_hash: str,
    parent_checkpoint_hash: str | None,
    state: dict[str, Any],
    raw_responses: list[dict[str, Any]],
    published_artifacts: list[Path],
) -> dict[str, Any]:
    """Build, but do not write, an actual M3 v2 checkpoint after an API scope passes."""

    chapter_id = str(chapter["chapter_id"])
    payload = {
        "stage": M3_V2_STAGE,
        "chapter_id": chapter_id,
        "chapter_index": chapter_index,
        "chapter_sequence_prefix": list(chapter_sequence_prefix),
        "source_hash": chapter_source_hash(chapter),
        "prompt_hashes": _m3_v2_prompt_hashes(design_doc, chapter_id),
        "config_hash": _m3_v2_config_hash(config),
        "schema_version": M3_V2_CHECKPOINT_SCHEMA_VERSION,
        "parent_checkpoint_hash": parent_checkpoint_hash,
        "input_m1_checkpoint_hash": input_m1_checkpoint_hash,
        "input_m2_checkpoint_hash": input_m2_checkpoint_hash,
        "state": state,
        "raw_responses": raw_responses,
        "artifact_manifest": artifact_manifest(published_artifacts, root=out_dir),
    }
    return build_checkpoint(payload)


def write_m3_v2_checkpoint_atomic(out_dir: Path, checkpoint: dict[str, Any]) -> Path:
    path = _m3_v2_checkpoint_path(out_dir, str(checkpoint["chapter_id"]))
    write_checkpoint_atomic(path, checkpoint)
    return path


class M3V2SemanticGateError(RuntimeError):
    """A semantic B4 response failed and must not be regenerated automatically."""

    def __init__(self, code: str, errors: list[str] | None = None) -> None:
        self.code = code
        self.errors = list(errors or [])
        detail = "; ".join(self.errors[:8])
        super().__init__(f"{code}: {detail}" if detail else code)


class M3V2TechnicalGateError(RuntimeError):
    """A transport or parse failure exhausted the allowed technical retry path."""

    def __init__(
        self,
        code: str,
        errors: list[str] | None = None,
        *,
        accounting: dict[str, int | float] | None = None,
    ) -> None:
        self.code = code
        self.errors = list(errors or [])
        self.accounting = dict(accounting or {})
        detail = "; ".join(self.errors[:8])
        super().__init__(f"{code}: {detail}" if detail else code)


def make_m3_v2_request_llm(client: LLMClient) -> RequestLLM:
    """Adapt the shared cache-backed client to M3 v2's in-loop request contract."""

    def request(messages: list[dict[str, str]], meta: dict[str, Any]) -> LLMResult:
        return client.call(
            messages,
            response_format=RESPONSE_FORMAT_JSON,
            tag=str(meta["tag"]),
            bypass_cache=bool(meta.get("bypass_cache", False)),
        )

    return request


def empty_m3_v2_state() -> dict[str, Any]:
    """Canonical state persisted in every M3 v2 checkpoint."""

    return {
        "entities": [],
        "atom_to_entity": {},
        "atom_catalog": {},
        "hint_to_entities": {},
        "relation_facts": [],
        "relation_phases": [],
        "blocked_for_runtime_pairs": [],
        "speaker_turns": [],
        "relation_events": [],
        "review_only": [],
    }


def _copy_state(state: dict[str, Any] | None) -> dict[str, Any]:
    return copy.deepcopy(state if state is not None else empty_m3_v2_state())


def _state_entities_by_id(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entity in state.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        entity_id = str(entity.get("entity_id") or "")
        if entity_id:
            result[entity_id] = entity
    return result


def _canonical_pair(values: Iterable[Any]) -> tuple[str, str] | None:
    """Return a stable pair key without inferring anything about its members."""

    items = [str(value) for value in values if str(value)]
    if len(items) != 2 or items[0] == items[1]:
        return None
    return tuple(sorted(items))


def _phase_row_pair(row: dict[str, Any]) -> tuple[str, str] | None:
    return _canonical_pair(row.get("pair") or [])


def _fact_row_pair(row: dict[str, Any]) -> tuple[str, str] | None:
    return _canonical_pair([row.get("subject_ref"), row.get("object_ref")])


def _blocked_runtime_pair_keys(state: dict[str, Any]) -> set[tuple[str, str]]:
    """Read persisted pair quarantines while remaining compatible with old checkpoints."""

    return {
        pair
        for row in state.get("blocked_for_runtime_pairs") or []
        if isinstance(row, dict)
        for pair in [_canonical_pair(row.get("pair") or [])]
        if pair is not None
    }


def _mint_entity_id(
    *,
    canonical_atom: dict[str, Any],
    occupied_ids: set[str],
) -> str:
    """Mint a readable deterministic id without treating two equal surfaces as equal."""

    base = _surface_key(str(canonical_atom.get("surface") or "")) or "entity"
    preferred = f"ent_{base}"
    if preferred not in occupied_ids:
        return preferred
    suffix = canonical_hash(str(canonical_atom.get("atom_id") or ""))[:10]
    candidate = f"{preferred}_{suffix}"
    if candidate in occupied_ids:
        raise M3V2SemanticGateError("stable_id_mint_collision", [candidate])
    return candidate


def _dedupe_aliases(aliases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, tuple[str, ...], str, str | None]] = set()
    output: list[dict[str, Any]] = []
    for alias in aliases:
        key = (
            str(alias.get("surface") or ""),
            tuple(sorted(str(item) for item in alias.get("member_atom_ids") or [])),
            str(alias.get("valid_from_block") or ""),
            str(alias.get("valid_until_block")) if alias.get("valid_until_block") is not None else None,
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(alias)
    return output


def _identity_atom_id_maps(
    atoms: list[dict[str, Any]],
) -> tuple[set[str], dict[str, set[str]]]:
    """Index atom ids by their redundant mention-prefix for mechanical repairs."""

    atom_ids = {str(atom["atom_id"]) for atom in atoms}
    mention_prefix_to_full: dict[str, set[str]] = {}
    for atom in atoms:
        full_id = str(atom["atom_id"])
        prefixes = {full_id.split("__", 1)[0]}
        mention_id = str(atom.get("mention_id") or "")
        if mention_id:
            prefixes.add(f"atom_{mention_id}")
        for prefix in prefixes:
            mention_prefix_to_full.setdefault(prefix, set()).add(full_id)
    return atom_ids, mention_prefix_to_full


def _repair_identity_atom_id(
    raw_id: Any,
    *,
    atom_ids: set[str],
    mention_prefix_to_full: dict[str, set[str]],
) -> tuple[Any, str | None]:
    """Repair only a uniquely resolvable omitted or incorrect block suffix.

    The prefix is the model-visible mention id.  A replacement is therefore
    bookkeeping only when that prefix names exactly one atom in this shard.
    """

    value = str(raw_id)
    if value in atom_ids:
        return raw_id, None
    prefix = value.split("__", 1)[0]
    candidates = mention_prefix_to_full.get(prefix) or set()
    if len(candidates) != 1:
        return raw_id, None
    repaired = next(iter(candidates))
    return repaired, "suffix" if "__" in value else "short"


def _normalize_identity_response_atom_ids(
    response: dict[str, Any],
    *,
    atoms: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Normalize narrow atom-id copy slips before the partition validator runs."""

    atom_ids, mention_prefix_to_full = _identity_atom_id_maps(atoms)
    normalized = copy.deepcopy(response)
    audit = {
        "evidence_atom_id_normalized": 0,
        "atom_id_suffix_repaired": 0,
        "atom_id_suffix_repaired_members": 0,
        "atom_id_suffix_repaired_evidence": 0,
        "atom_id_suffix_repaired_aliases": 0,
    }

    def repair_list(values: Any, *, destination: str) -> Any:
        if not isinstance(values, list):
            return values
        repaired_values: list[Any] = []
        for raw_id in values:
            repaired, repair_kind = _repair_identity_atom_id(
                raw_id,
                atom_ids=atom_ids,
                mention_prefix_to_full=mention_prefix_to_full,
            )
            repaired_values.append(repaired)
            if repair_kind == "short":
                if destination == "evidence":
                    audit["evidence_atom_id_normalized"] += 1
            elif repair_kind == "suffix":
                if destination == "aliases":
                    # Alias bindings duplicate group membership. Surface the
                    # repair without inflating the primary member/evidence
                    # counter used to compare model copy slips across runs.
                    audit["atom_id_suffix_repaired_aliases"] += 1
                else:
                    audit["atom_id_suffix_repaired"] += 1
                    audit[f"atom_id_suffix_repaired_{destination}"] += 1
        return repaired_values

    for group in normalized.get("groups") or []:
        if not isinstance(group, dict):
            continue
        group["member_atom_ids"] = repair_list(
            group.get("member_atom_ids"), destination="members"
        )
        for alias in group.get("alias_bindings") or []:
            if isinstance(alias, dict):
                alias["member_atom_ids"] = repair_list(
                    alias.get("member_atom_ids"), destination="aliases"
                )
        for evidence in group.get("evidence") or []:
            if isinstance(evidence, dict):
                evidence["source_atom_ids"] = repair_list(
                    evidence.get("source_atom_ids"), destination="evidence"
                )
    return normalized, audit


def _normalize_identity_responses_by_shard(
    responses: list[dict[str, Any]],
    *,
    identity_shards: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Enforce per-shard assignment authority before responses are combined.

    A member atom outside the shard's request is not a competing identity
    verdict. The model was never authorized to assign it, so the membership is
    removed while the raw response and any cross-shard evidence remain intact.
    The later full-scope exact-partition gate proves that the owning shard did
    actually assign every real atom.
    """

    if len(responses) != len(identity_shards):
        raise ValueError(
            "M3 v2 identity response/shard count mismatch: "
            f"responses={len(responses)} shards={len(identity_shards)}"
        )

    normalized_responses: list[dict[str, Any]] = []
    audit = {
        "evidence_atom_id_normalized": 0,
        "atom_id_suffix_repaired": 0,
        "atom_id_suffix_repaired_members": 0,
        "atom_id_suffix_repaired_evidence": 0,
        "atom_id_suffix_repaired_aliases": 0,
        "member_out_of_shard_stripped": 0,
    }
    for response, shard in zip(responses, identity_shards, strict=True):
        local_atoms = [item for item in shard.get("items") or [] if isinstance(item, dict)]
        cleaned, local_audit = _normalize_identity_response_atom_ids(response, atoms=local_atoms)
        for key, value in local_audit.items():
            audit[key] = int(audit.get(key, 0)) + int(value)

        allowed_atom_ids = {str(atom.get("atom_id") or "") for atom in local_atoms}
        allowed_atom_ids.discard("")
        for group in cleaned.get("groups") or []:
            if not isinstance(group, dict):
                continue
            members = [str(value) for value in group.get("member_atom_ids") or [] if str(value)]
            foreign_members = [atom_id for atom_id in members if atom_id not in allowed_atom_ids]
            if not foreign_members:
                continue

            kept_members = [atom_id for atom_id in members if atom_id in allowed_atom_ids]
            kept_member_ids = set(kept_members)
            group["member_atom_ids"] = kept_members
            group["_jurisdiction_stripped_member_atom_ids"] = sorted(set(foreign_members))
            audit["member_out_of_shard_stripped"] += len(foreign_members)

            # Alias bindings are membership claims too. Drop a binding only
            # when all of its supporting members were void in this shard;
            # evidence remains preserved as auditable cross-shard provenance.
            aliases_raw = group.get("alias_bindings")
            if not isinstance(aliases_raw, list):
                continue
            aliases: list[dict[str, Any]] = []
            for alias in aliases_raw:
                if not isinstance(alias, dict):
                    aliases.append(alias)
                    continue
                alias_copy = copy.deepcopy(alias)
                alias_copy["member_atom_ids"] = [
                    str(value)
                    for value in alias_copy.get("member_atom_ids") or []
                    if str(value) in kept_member_ids
                ]
                if alias_copy["member_atom_ids"]:
                    aliases.append(alias_copy)
            group["alias_bindings"] = aliases
        normalized_responses.append(cleaned)
    return normalized_responses, audit


def _quarantine_out_of_enum_referent_kinds(response: dict[str, Any]) -> dict[str, int]:
    """Keep an ontology miss reviewable without mapping it to an allowed class."""

    audit = {"referent_kind_out_of_enum": 0}
    for group in response.get("groups") or []:
        if not isinstance(group, dict):
            continue
        raw_kind = str(group.get("referent_kind") or "").strip()
        if not raw_kind or raw_kind in IDENTITY_REFERENT_KINDS:
            continue
        group["referent_kind_raw"] = raw_kind
        group["referent_kind"] = "unknown"
        group["status"] = "quarantine"
        if group.get("reuse_entity_id") is not None:
            group["reuse_entity_id_raw"] = group["reuse_entity_id"]
            group["reuse_entity_id"] = None
        audit["referent_kind_out_of_enum"] += 1
    return audit


def normalize_identity_evidence_atom_ids(
    response: dict[str, Any],
    *,
    atoms: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Compatibility wrapper for Amendment #1's evidence-only counter."""

    normalized, audit = _normalize_identity_response_atom_ids(response, atoms=atoms)
    return normalized, audit["evidence_atom_id_normalized"]


def _normalize_identity_reuse_ids(
    response: dict[str, Any],
    *,
    atoms_by_id: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, int]:
    """Resolve only recorded hint bindings or a contentless deterministic mint claim."""

    existing_ids = set(_state_entities_by_id(state))
    occupied_ids = set(existing_ids)
    hint_to_entities = state.get("hint_to_entities") or {}
    audit = {"reuse_hint_normalized": 0, "reuse_mint_equivalent": 0}

    for group in response.get("groups") or []:
        if not isinstance(group, dict):
            continue
        canonical_atom = atoms_by_id.get(str(group.get("canonical_atom_id") or ""))
        reuse = group.get("reuse_entity_id")
        if reuse is not None:
            claimed_id = str(reuse)
            if claimed_id not in existing_ids:
                hint_targets = [str(item) for item in hint_to_entities.get(claimed_id) or []]
                if len(hint_targets) == 1 and hint_targets[0] in existing_ids:
                    group["reuse_entity_id"] = hint_targets[0]
                    audit["reuse_hint_normalized"] += 1
                elif canonical_atom is not None:
                    expected_mint = _mint_entity_id(
                        canonical_atom=canonical_atom,
                        occupied_ids=occupied_ids,
                    )
                    if claimed_id == expected_mint:
                        group["reuse_entity_id"] = None
                        audit["reuse_mint_equivalent"] += 1

        if group.get("reuse_entity_id") is None and canonical_atom is not None:
            occupied_ids.add(
                _mint_entity_id(canonical_atom=canonical_atom, occupied_ids=occupied_ids)
            )
    return audit


def _duplicate_reuse_union_count(response: dict[str, Any]) -> int:
    """Permit compatible repeated reuse claims; reject an explicit split witness."""

    claims: dict[str, list[dict[str, Any]]] = {}
    group_by_atom: dict[str, int] = {}
    for group_index, group in enumerate(response.get("groups") or []):
        if not isinstance(group, dict):
            continue
        reuse = group.get("reuse_entity_id")
        if reuse is not None:
            claims.setdefault(str(reuse), []).append(group)
        for atom_id in group.get("member_atom_ids") or []:
            group_by_atom[str(atom_id)] = group_index

    unions = 0
    for entity_id, rows in claims.items():
        if len(rows) < 2:
            continue
        if len({str(row.get("referent_kind") or "") for row in rows}) != 1:
            raise M3V2SemanticGateError("stable_id_split_tie", [entity_id, "referent_kind"])
        duplicate_group_indexes = {
            index
            for index, group in enumerate(response.get("groups") or [])
            if isinstance(group, dict) and str(group.get("reuse_entity_id") or "") == entity_id
        }
        for group in response.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for evidence in group.get("evidence") or []:
                if not isinstance(evidence, dict) or evidence.get("supports") != "different_identity":
                    continue
                touched_groups = {
                    group_by_atom[str(atom_id)]
                    for atom_id in evidence.get("source_atom_ids") or []
                    if str(atom_id) in group_by_atom
                }
                if len(touched_groups & duplicate_group_indexes) > 1:
                    raise M3V2SemanticGateError("stable_id_split_tie", [entity_id, "different_identity"])
        unions += len(rows) - 1
    return unions


def count_identity_evidence_cross_group_source_atoms(response: dict[str, Any]) -> int:
    """Count valid cross-group provenance retained for review in accepted evidence."""

    count = 0
    for group in response.get("groups") or []:
        if not isinstance(group, dict):
            continue
        members = {str(value) for value in group.get("member_atom_ids") or [] if str(value)}
        for evidence in group.get("evidence") or []:
            if not isinstance(evidence, dict) or evidence.get("supports") != "same_identity":
                continue
            count += sum(
                str(atom_id) not in members
                for atom_id in (evidence.get("source_atom_ids") or [])
                if str(atom_id)
            )
    return count


def apply_identity_partition_response(
    state: dict[str, Any],
    response: dict[str, Any],
    *,
    atoms: list[dict[str, Any]],
    source_text_by_block: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a validated identity partition to a copy of the prior state.

    The function makes no linguistic inference.  It either uses an explicit,
    valid ``reuse_entity_id`` or mints an id from the model-selected canonical
    atom.  Two groups claiming one old id are an unresolvable split tie and halt.
    """

    working = _copy_state(state)
    entities_by_id = _state_entities_by_id(working)
    normalized_response, atom_id_audit = _normalize_identity_response_atom_ids(
        response,
        atoms=atoms,
    )
    referent_kind_audit = _quarantine_out_of_enum_referent_kinds(normalized_response)
    atoms_by_id = {str(atom["atom_id"]): atom for atom in atoms}
    reuse_normalization_audit = _normalize_identity_reuse_ids(
        normalized_response,
        atoms_by_id=atoms_by_id,
        state=working,
    )
    cross_group_source_atom_count = count_identity_evidence_cross_group_source_atoms(
        normalized_response
    )
    quote_audit: dict[str, int] = {"evidence_quote_punct_normalized": 0}
    errors = validate_identity_partition_response(
        normalized_response,
        atoms=atoms,
        prior_entity_ids=set(entities_by_id),
        source_text_by_block=source_text_by_block,
        quote_audit=quote_audit,
    )
    if errors:
        raise M3V2SemanticGateError("identity_response_rejected", errors)

    groups = list(normalized_response.get("groups") or [])
    duplicate_reuse_unions = _duplicate_reuse_union_count(normalized_response)
    audit = {
        "groups_applied": 0,
        "entities_minted": 0,
        "entities_reused": 0,
        "review_only_groups": 0,
        "blocked_for_runtime": 0,
        "supersedes": [],
        **atom_id_audit,
        **referent_kind_audit,
        **reuse_normalization_audit,
        "reuse_duplicate_unions": duplicate_reuse_unions,
        "evidence_cross_group_source_atoms": cross_group_source_atom_count,
        "evidence_quote_punct_normalized": quote_audit["evidence_quote_punct_normalized"],
    }
    occupied_ids = set(entities_by_id)
    for group in groups:
        member_ids = [str(value) for value in group["member_atom_ids"]]
        canonical_atom = atoms_by_id[str(group["canonical_atom_id"])]
        reuse = group.get("reuse_entity_id")
        if reuse is not None:
            entity_id = str(reuse)
            entity = entities_by_id[entity_id]
            if str(entity.get("referent_kind") or "") != str(group["referent_kind"]):
                raise M3V2SemanticGateError(
                    "identity_reuse_kind_collision",
                    [entity_id, str(entity.get("referent_kind")), str(group.get("referent_kind"))],
                )
            audit["entities_reused"] += 1
        else:
            entity_id = _mint_entity_id(canonical_atom=canonical_atom, occupied_ids=occupied_ids)
            occupied_ids.add(entity_id)
            entity = {
                "entity_id": entity_id,
                "canonical": str(canonical_atom["surface"]),
                "canonical_atom_id": str(canonical_atom["atom_id"]),
                "referent_kind": str(group["referent_kind"]),
                "member_atom_ids": [],
                "aliases": [],
                "supersedes_entity_ids": [],
                "status": str(group["status"]),
            }
            working["entities"].append(entity)
            entities_by_id[entity_id] = entity
            audit["entities_minted"] += 1

        if str(group.get("status") or "") != "resolved" or str(group.get("referent_kind") or "") != "person":
            audit["review_only_groups"] += 1
            working["review_only"].append(
                {
                    "kind": "identity_group",
                    "entity_id": entity_id,
                    "referent_kind": str(group.get("referent_kind") or ""),
                    "referent_kind_raw": group.get("referent_kind_raw"),
                    "status": str(group.get("status") or ""),
                    "member_atom_ids": member_ids,
                }
            )

        entity["member_atom_ids"] = sorted(
            set(str(value) for value in entity.get("member_atom_ids") or []) | set(member_ids)
        )
        entity["aliases"] = _dedupe_aliases(
            [*(entity.get("aliases") or []), *copy.deepcopy(group.get("alias_bindings") or [])]
        )
        for atom_id in member_ids:
            atom = atoms_by_id[atom_id]
            working["atom_to_entity"][atom_id] = entity_id
            working["atom_catalog"][atom_id] = copy.deepcopy(atom)
        audit["groups_applied"] += 1

    hint_to_entities: dict[str, set[str]] = {}
    for atom_id, entity_id in (working.get("atom_to_entity") or {}).items():
        atom = (working.get("atom_catalog") or {}).get(atom_id) or {}
        hint = str(atom.get("hint_entity_id") or "")
        if hint:
            hint_to_entities.setdefault(hint, set()).add(str(entity_id))
    working["hint_to_entities"] = {
        hint: sorted(entity_ids) for hint, entity_ids in sorted(hint_to_entities.items())
    }
    return working, audit


def _resolve_final_entity_ref(
    state: dict[str, Any],
    ref: Any,
    *,
    block_id: str | None = None,
) -> str | None:
    """Resolve only an explicit B1/B2 hint or exact same-block atom surface."""

    entity_ids = set(_state_entities_by_id(state))
    raw = str(ref or "") if isinstance(ref, str) else ""
    if raw in entity_ids:
        return raw
    if raw:
        hinted = (state.get("hint_to_entities") or {}).get(raw) or []
        if len(hinted) == 1:
            return str(hinted[0])
    if isinstance(ref, dict):
        candidates = [str(value) for value in ref.get("candidate_entity_ids") or [] if str(value)]
        if len(candidates) == 1:
            resolved = (state.get("hint_to_entities") or {}).get(candidates[0]) or []
            if len(resolved) == 1:
                return str(resolved[0])
        raw = str(ref.get("surface") or "")
    if not raw or not block_id:
        return None
    matched = {
        str(entity_id)
        for atom_id, entity_id in (state.get("atom_to_entity") or {}).items()
        if str(((state.get("atom_catalog") or {}).get(atom_id) or {}).get("block_id") or "") == str(block_id)
        and _surface_key(str(((state.get("atom_catalog") or {}).get(atom_id) or {}).get("surface") or ""))
        == _surface_key(raw)
    }
    return next(iter(matched)) if len(matched) == 1 else None


def _derive_provisional_bindings(
    state: dict[str, Any],
    phase_rows: list[dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Compose existing B2 and identity verdicts into scope-local id bindings.

    The routine never compares names or otherwise judges identity. A provisional
    id can bind only when its own event side resolves through the existing
    same-block/hint paths, and every usable witness points to the same final id.
    """

    provisional_ids = sorted(
        {
            str(value)
            for row in phase_rows
            for value in row.get("provisional_pair") or []
            if str(value) and _resolve_final_entity_ref(state, str(value)) is None
        }
    )
    bindings: dict[str, str] = {}
    audit_rows: list[dict[str, Any]] = []

    for provisional_id in provisional_ids:
        candidate_final_ids: set[str] = set()
        witnesses: list[dict[str, str]] = []
        for row in phase_rows:
            pair = [str(value) for value in row.get("provisional_pair") or []]
            if len(pair) != 2 or provisional_id not in pair:
                continue
            pair_mate = pair[1] if pair[0] == provisional_id else pair[0]
            pair_mate_final = _resolve_final_entity_ref(state, pair_mate)
            if pair_mate_final is None:
                continue
            for history in row.get("history") or []:
                for event in history.get("events") or []:
                    if not isinstance(event, dict):
                        continue
                    block_id = str(event.get("block_id") or "")
                    for side_name, other_side_name in [("actor", "target"), ("target", "actor")]:
                        side = event.get(side_name) or {}
                        other_side = event.get(other_side_name) or {}
                        if not isinstance(side, dict) or not isinstance(other_side, dict):
                            continue
                        direct_candidate = [
                            str(value) for value in side.get("candidate_entity_ids") or [] if str(value)
                        ] == [provisional_id]
                        other_final = _resolve_final_entity_ref(
                            state, other_side, block_id=block_id
                        )
                        by_elimination = other_final == pair_mate_final
                        if not direct_candidate and not by_elimination:
                            continue
                        final_id = _resolve_final_entity_ref(state, side, block_id=block_id)
                        if final_id is None:
                            continue
                        candidate_final_ids.add(final_id)
                        witnesses.append(
                            {
                                "event_id": str(event.get("event_id") or ""),
                                "block_id": block_id,
                                "side": side_name,
                                "method": (
                                    "candidate_entity_id"
                                    if direct_candidate
                                    else "pair_mate_elimination"
                                ),
                                "final_entity_id": final_id,
                            }
                        )
        if len(candidate_final_ids) != 1:
            continue
        final_id = next(iter(candidate_final_ids))
        bindings[provisional_id] = final_id
        audit_rows.append(
            {
                "provisional_id": provisional_id,
                "final_entity_id": final_id,
                "witnesses": sorted(
                    witnesses,
                    key=lambda item: (
                        item["block_id"],
                        item["event_id"],
                        item["side"],
                    ),
                ),
            }
        )
    return bindings, audit_rows


def _remap_phase_rows_to_final_ids(
    state: dict[str, Any],
    phase_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Map only unambiguous evidence into the phase stage; preserve residuals for review."""

    provisional_bindings, binding_witnesses = _derive_provisional_bindings(state, phase_rows)

    def resolve(ref: str) -> str | None:
        return _resolve_final_entity_ref(state, ref) or provisional_bindings.get(ref)

    mapped: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for row in phase_rows:
        pair = [str(value) for value in row.get("provisional_pair") or []]
        if len(pair) != 2:
            unresolved.append({"reason": "invalid_provisional_pair", "row": row})
            continue
        final_pair = [
            resolve(pair[0]),
            resolve(pair[1]),
        ]
        if None in final_pair or final_pair[0] == final_pair[1]:
            unresolved.append({"reason": "unresolved_or_collapsed_pair", "row": row})
            continue
        mapped.append({**copy.deepcopy(row), "pair": sorted(str(value) for value in final_pair)})
    return mapped, unresolved, {
        "provisional_bindings": len(provisional_bindings),
        "provisional_binding_witnesses": binding_witnesses,
    }


def _partition_phase_response_by_pair(
    response: dict[str, Any],
    *,
    allowed_pairs: set[tuple[str, str]],
) -> tuple[dict[tuple[str, str], dict[str, list[dict[str, Any]]]], list[str]]:
    """Group model rows by a declared input pair before pair-local validation.

    A row that cannot be assigned to an input pair remains a whole-scope gate.  It
    cannot be safely quarantined because doing so would silently accept a model
    invented relation endpoint.
    """

    if not isinstance(response, dict):
        return {}, ["response must be object"]
    phases = response.get("relation_phases")
    facts = response.get("relation_facts")
    errors: list[str] = []
    if not isinstance(phases, list):
        errors.append("relation_phases must be list")
        phases = []
    if not isinstance(facts, list):
        errors.append("relation_facts must be list")
        facts = []

    grouped: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}

    def add_row(
        pair: tuple[str, str],
        field: str,
        row: dict[str, Any],
    ) -> None:
        grouped.setdefault(pair, {"relation_phases": [], "relation_facts": []})[field].append(
            copy.deepcopy(row)
        )

    for index, row in enumerate(phases):
        prefix = f"relation_phases[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix} must be object")
            continue
        pair = _phase_row_pair(row)
        if pair is None:
            errors.append(f"{prefix}.pair invalid")
        elif pair not in allowed_pairs:
            errors.append(f"{prefix}.pair not_in_input_batch")
        else:
            add_row(pair, "relation_phases", row)
    for index, row in enumerate(facts):
        prefix = f"relation_facts[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix} must be object")
            continue
        pair = _fact_row_pair(row)
        if pair is None:
            errors.append(f"{prefix}.subject_or_object invalid")
        elif pair not in allowed_pairs:
            errors.append(f"{prefix}.subject_or_object not_in_input_batch")
        else:
            add_row(pair, "relation_facts", row)
    return grouped, errors


def _replace_blocked_runtime_pair_records(
    state: dict[str, Any],
    *,
    cleared_pairs: set[tuple[str, str]],
    new_records: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """Update pair quarantines without changing retained relation history."""

    records = {
        pair: copy.deepcopy(row)
        for row in state.get("blocked_for_runtime_pairs") or []
        if isinstance(row, dict)
        for pair in [_canonical_pair(row.get("pair") or [])]
        if pair is not None and pair not in cleared_pairs
    }
    records.update(new_records)
    state["blocked_for_runtime_pairs"] = [
        records[pair] for pair in sorted(records)
    ]
    state["review_only"] = [
        row
        for row in state.get("review_only") or []
        if not (
            isinstance(row, dict)
            and row.get("kind") == "relation_pair_blocked_for_runtime"
            and _canonical_pair(row.get("pair") or []) in cleared_pairs
        )
    ]
    state["review_only"].extend(
        copy.deepcopy(record) for _pair, record in sorted(new_records.items())
    )


def apply_phase_segment_response(
    state: dict[str, Any],
    response: dict[str, Any],
    *,
    allowed_pairs: set[tuple[str, str]],
    source_text_by_block: dict[str, str],
    block_ordinals: dict[str, int],
    scope_end_block: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply valid relation pairs and quarantine only pairs with semantic rejects.

    The model's raw response remains immutable on disk.  This function only
    applies deterministic validation results: a rejected pair is retained for
    human review and excluded from runtime materialization, while unrelated
    pairs in the same response can still publish.
    """

    working = _copy_state(state)
    entity_ids = set(_state_entities_by_id(working))
    affected = {pair for pair in (_canonical_pair(values) for values in allowed_pairs) if pair is not None}
    pair_payloads, grouping_errors = _partition_phase_response_by_pair(
        response,
        allowed_pairs=affected,
    )
    if grouping_errors:
        raise M3V2SemanticGateError("phase_response_rejected", grouping_errors)

    quote_normalized = 0
    blocked_records: dict[tuple[str, str], dict[str, Any]] = {}
    valid_payloads: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    prior_phase_rows = list(working.get("relation_phases") or [])
    prior_fact_rows = list(working.get("relation_facts") or [])
    for pair, payload in pair_payloads.items():
        quote_audit: dict[str, int] = {"evidence_quote_punct_normalized": 0}
        errors = validate_phase_segment_response(
            payload,
            entity_ids=entity_ids,
            source_text_by_block=source_text_by_block,
            block_ordinals=block_ordinals,
            allowed_pairs={pair},
            scope_end_block=scope_end_block,
            quote_audit=quote_audit,
        )
        quote_normalized += int(quote_audit["evidence_quote_punct_normalized"])
        if not errors:
            valid_payloads[pair] = payload
            continue
        retained_phases = [
            copy.deepcopy(row)
            for row in prior_phase_rows
            if isinstance(row, dict) and _phase_row_pair(row) == pair
        ]
        retained_facts = [
            copy.deepcopy(row)
            for row in prior_fact_rows
            if isinstance(row, dict) and _fact_row_pair(row) == pair
        ]
        blocked_records[pair] = {
            "kind": "relation_pair_blocked_for_runtime",
            "pair": list(pair),
            "status": "blocked_for_runtime",
            "needs_human_review": True,
            "reject_reasons": list(errors),
            "returned_relation_phases": copy.deepcopy(payload["relation_phases"]),
            "returned_relation_facts": copy.deepcopy(payload["relation_facts"]),
            "prior_history_retained": bool(retained_phases or retained_facts),
            "retained_relation_phases": retained_phases,
            "retained_relation_facts": retained_facts,
        }

    if pair_payloads and len(blocked_records) == len(pair_payloads):
        raise M3V2SemanticGateError(
            "phase_all_pairs_blocked_for_runtime",
            [
                f"pair={list(pair)}: {'; '.join(record['reject_reasons'])}"
                for pair, record in sorted(blocked_records.items())
            ],
        )

    replace_pairs = affected - set(blocked_records)
    working["relation_facts"] = [
        row
        for row in working.get("relation_facts") or []
        if not isinstance(row, dict) or _fact_row_pair(row) not in replace_pairs
    ]
    working["relation_phases"] = [
        row
        for row in working.get("relation_phases") or []
        if not isinstance(row, dict) or _phase_row_pair(row) not in replace_pairs
    ]
    facts_applied = 0
    phases_applied = 0
    review_only_facts = 0
    for _pair in sorted(valid_payloads):
        payload = valid_payloads[_pair]
        for fact in payload["relation_facts"]:
            if fact.get("predicate_code") == "other":
                fact["status"] = "review_only"
                working["review_only"].append({"kind": "relation_fact", **copy.deepcopy(fact)})
                review_only_facts += 1
            else:
                fact["status"] = "published"
            working["relation_facts"].append(fact)
            facts_applied += 1
        for phase in payload["relation_phases"]:
            phase["status"] = str(phase.get("status") or "open")
            working["relation_phases"].append(phase)
            phases_applied += 1
    _replace_blocked_runtime_pair_records(
        working,
        cleared_pairs=affected,
        new_records=blocked_records,
    )
    return working, {
        "pairs_replayed": len(affected),
        "facts_applied": facts_applied,
        "phases_applied": phases_applied,
        "review_only_facts": review_only_facts,
        "pairs_blocked_for_runtime": len(blocked_records),
        "blocked_pair_fact_rows": sum(
            len(record["returned_relation_facts"]) for record in blocked_records.values()
        ),
        "evidence_quote_punct_normalized": quote_normalized,
    }


def _asof_interactions(
    *,
    m1_dir: Path,
    m1_checkpoints: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep every B2 turn/event; unresolved references remain explicit rather than dropped."""

    turns: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for artifact_path in _manifest_paths(
        root=Path(m1_dir), checkpoints=m1_checkpoints, directory="narrative"
    ):
        parsed = _clean_parsed_payload(artifact_path)
        for turn in parsed.get("speaker_turns") or []:
            if not isinstance(turn, dict):
                continue
            row = copy.deepcopy(turn)
            block_id = str(row.get("block_id") or "")
            row["speaker_entity_id"] = _resolve_final_entity_ref(
                state, row.get("speaker"), block_id=block_id
            )
            row["addressee_entity_id"] = _resolve_final_entity_ref(
                state, row.get("addressee"), block_id=block_id
            )
            turns.append(row)
        for event in parsed.get("relation_events") or []:
            if not isinstance(event, dict):
                continue
            row = copy.deepcopy(event)
            block_id = str(row.get("block_id") or "")
            row["actor_entity_id"] = _resolve_final_entity_ref(
                state, row.get("actor"), block_id=block_id
            )
            row["target_entity_id"] = _resolve_final_entity_ref(
                state, row.get("target"), block_id=block_id
            )
            events.append(row)
    turns.sort(key=lambda row: (str(row.get("block_id") or ""), str(row.get("turn_id") or "")))
    events.sort(key=lambda row: (str(row.get("block_id") or ""), str(row.get("event_id") or "")))
    return turns, events


def _observed_address_policies(state: dict[str, Any]) -> list[dict[str, Any]]:
    observed: dict[tuple[str, str], list[str]] = {}
    for turn in state.get("speaker_turns") or []:
        source = str(turn.get("speaker_entity_id") or "")
        target = str(turn.get("addressee_entity_id") or "")
        term = str(turn.get("address_term_used") or "").strip()
        if source and target and term:
            observed.setdefault((source, target), [])
            if term not in observed[(source, target)]:
                observed[(source, target)].append(term)
    blocked_pairs = _blocked_runtime_pair_keys(state)
    rows: list[dict[str, Any]] = []
    for phase in state.get("relation_phases") or []:
        pair = [str(value) for value in phase.get("pair") or []]
        if len(pair) != 2 or _canonical_pair(pair) in blocked_pairs:
            continue
        rows.append(
            {
                "pair": pair,
                "phase_ref": f"{phase.get('phase_label')}@{phase.get('valid_from_block')}",
                "a_to_b": {
                    "observed_terms": observed.get((pair[0], pair[1]), []),
                    "evidence_level": "observed" if observed.get((pair[0], pair[1])) else "unsupported",
                    "needs_human_review": True,
                    "runtime_usable": False,
                },
                "b_to_a": {
                    "observed_terms": observed.get((pair[1], pair[0]), []),
                    "evidence_level": "observed" if observed.get((pair[1], pair[0])) else "unsupported",
                    "needs_human_review": True,
                    "runtime_usable": False,
                },
                "proposal_only": True,
            }
        )
    return rows


def _glossary_as_of(m1_dir: Path, m1_checkpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for artifact_path in _manifest_paths(
        root=Path(m1_dir), checkpoints=m1_checkpoints, directory="lexicon"
    ):
        parsed = _clean_parsed_payload(artifact_path)
        for term in parsed.get("glossary_candidates") or []:
            if not isinstance(term, dict):
                continue
            source = str(term.get("source_term") or "").strip()
            if not source:
                continue
            key = _surface_key(source)
            row = rows.setdefault(
                key,
                {
                    "source_term": source,
                    "proposed_target_vi": str(term.get("proposed_target_vi") or ""),
                    "category": str(term.get("category") or "other"),
                    "block_ids": [],
                    "status": "candidate",
                },
            )
            row["block_ids"] = sorted(
                set(str(value) for value in row["block_ids"] + list(term.get("block_ids") or []))
            )
    return sorted(rows.values(), key=lambda row: str(row["source_term"]).casefold())


def build_story_bible_v2(
    *,
    chapter: dict[str, Any],
    state: dict[str, Any],
    m1_dir: Path,
    m1_checkpoints: list[dict[str, Any]],
    digests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Materialize the published, scope-bounded Story Bible view from approved state."""

    chapter_id = str(chapter["chapter_id"])
    block_ids = [str(block.get("block_id") or "") for block in chapter.get("blocks") or []]
    entities = [
        copy.deepcopy(entity)
        for entity in state.get("entities") or []
        if entity.get("referent_kind") == "person" and entity.get("status") == "resolved"
    ]
    person_ids = {str(entity["entity_id"]) for entity in entities}
    blocked_pairs = _blocked_runtime_pair_keys(state)
    facts = [
        copy.deepcopy(row)
        for row in state.get("relation_facts") or []
        if row.get("status") == "published"
        and str(row.get("subject_ref") or "") in person_ids
        and str(row.get("object_ref") or "") in person_ids
        and _fact_row_pair(row) not in blocked_pairs
    ]
    phases: list[dict[str, Any]] = []
    for row in state.get("relation_phases") or []:
        if (
            not set(str(value) for value in row.get("pair") or []).issubset(person_ids)
            or _phase_row_pair(row) in blocked_pairs
        ):
            continue
        rendered = copy.deepcopy(row)
        # ``valid_until_block`` is the state/internal interval field. Published
        # Story Bible artifacts expose one canonical boundary field only.
        rendered["valid_to_block"] = rendered.pop("valid_until_block", None)
        phases.append(rendered)
    narration_segments = [
        {"chapter_id": str(digest.get("chapter_id") or ""), **copy.deepcopy(segment)}
        for digest in digests
        for segment in digest.get("narration_frame_segments") or []
        if isinstance(segment, dict)
    ]
    return {
        "scope": f"M3_asof_{chapter_id}",
        "artifact_scope_end_block": block_ids[-1] if block_ids else "",
        "status": "partial_story_bible_v2",
        "registry_T1_glossary": _glossary_as_of(m1_dir, m1_checkpoints),
        "registry_T2_entities": entities,
        "registry_T3_speaker_turns": copy.deepcopy(state.get("speaker_turns") or []),
        "registry_T3_relation_events": copy.deepcopy(state.get("relation_events") or []),
        "registry_T4_chapter_digests": copy.deepcopy(digests),
        "relation_facts": facts,
        "entity_relations": phases,
        "address_policies": _observed_address_policies(state),
        "blocked_for_runtime_pairs": copy.deepcopy(state.get("blocked_for_runtime_pairs") or []),
        "narration_frame_segments": narration_segments,
        "review_only": copy.deepcopy(state.get("review_only") or []),
        "source_ranges": {"first_block": block_ids[0] if block_ids else "", "last_block": block_ids[-1] if block_ids else ""},
    }


def validate_story_bible_v2(
    story: dict[str, Any],
    *,
    expected_turn_count: int,
    expected_event_count: int,
) -> list[str]:
    """Semantic publish gate for the v2 artifact, without old pilot assumptions."""

    errors: list[str] = []
    entities = story.get("registry_T2_entities") or []
    entity_ids = [str(entity.get("entity_id") or "") for entity in entities if isinstance(entity, dict)]
    if len(entity_ids) != len(set(entity_ids)) or not all(entity_ids):
        errors.append("registry_T2 entity ids invalid_or_duplicate")
    if any(str(entity.get("referent_kind") or "") != "person" for entity in entities if isinstance(entity, dict)):
        errors.append("registry_T2 must be person_only")
    if len(story.get("registry_T3_speaker_turns") or []) != expected_turn_count:
        errors.append("speaker_turn_count changed during consolidation")
    if len([row for row in story.get("registry_T3_speaker_turns") or [] if isinstance(row, dict)]) != expected_turn_count:
        errors.append("speaker_turns malformed")
    if len(story.get("registry_T3_relation_events") or []) != expected_event_count:
        errors.append("relation_event_count changed during consolidation")
    if len([row for row in story.get("registry_T4_chapter_digests") or [] if isinstance(row, dict)]) == 0:
        errors.append("missing digests")
    for fact in story.get("relation_facts") or []:
        if fact.get("predicate_code") not in PREDICATE_CODES:
            errors.append("relation_fact predicate invalid")
        if fact.get("subject_ref") == fact.get("object_ref"):
            errors.append("relation_fact self_loop")
    for phase in story.get("entity_relations") or []:
        pair = [str(value) for value in phase.get("pair") or []]
        if len(pair) != 2 or pair[0] == pair[1] or any(value not in entity_ids for value in pair):
            errors.append("relation_phase pair invalid")
        if "valid_until_block" in phase:
            errors.append("relation_phase leaked internal valid_until_block")
        valid_to = phase.get("valid_to_block")
        if phase.get("status") == "closed" and not str(valid_to or ""):
            errors.append("closed relation_phase missing valid_to_block")
        if phase.get("status") == "open" and valid_to is not None:
            errors.append("open relation_phase has valid_to_block")
    if expected_event_count < 0:
        errors.append("event_count invalid")
    return errors


def _response_batch(
    payload: dict[str, Any],
    *,
    field: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    value = payload.get(field)
    if isinstance(value, dict):
        rows = [value]
    elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
        rows = list(value)
    else:
        raise M3V2SemanticGateError(f"{field}_responses_missing")
    if len(rows) != expected_count:
        raise M3V2SemanticGateError(
            f"{field}_response_shard_count",
            [f"expected={expected_count}", f"actual={len(rows)}"],
        )
    return rows


def _empty_m3_v2_request_accounting() -> dict[str, int | float]:
    return {
        "logical_calls": 0,
        "attempts": 0,
        "technical_retries": 0,
        "poisoned_cache_replays": 0,
        "cache_hits": 0,
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
    }


def _add_m3_v2_request_accounting(
    left: dict[str, int | float],
    right: dict[str, int | float],
) -> dict[str, int | float]:
    merged = _empty_m3_v2_request_accounting()
    for key in merged:
        value = float(left.get(key, 0)) + float(right.get(key, 0))
        merged[key] = round(value, 12) if key == "cost_usd" else int(value)
    return merged


def _m3_v2_raw_response_path(
    out_dir: Path,
    *,
    chapter_id: str,
    mode: str,
    shard_index: int,
    attempt_index: int,
    collision_index: int = 0,
) -> Path:
    """Return a collision-aware raw response path without reusing an old attempt."""

    suffix = "" if collision_index == 0 else f"_resume_{collision_index:02d}"
    return (
        Path(out_dir)
        / "raw_responses"
        / M3_V2_STAGE
        / chapter_id
        / f"{mode}_shard_{shard_index:02d}_attempt_{attempt_index:02d}{suffix}.json"
    )


def _append_only_json_write(path: Path, payload: dict[str, Any]) -> None:
    """Write immutable audit evidence with an exclusive create, never replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _m3_v2_usage_payload(result: LLMResult | None) -> dict[str, int]:
    if result is None:
        return {
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
        }
    return {
        "prompt_tokens": int(result.usage.prompt_tokens),
        "cached_tokens": int(result.usage.cached_tokens),
        "completion_tokens": int(result.usage.completion_tokens),
        "reasoning_tokens": int(result.usage.reasoning_tokens),
    }


def _persist_m3_v2_raw_response(
    out_dir: Path,
    *,
    messages: list[dict[str, str]],
    meta: dict[str, Any],
    source: str,
    result: LLMResult | None = None,
    provided_json: dict[str, Any] | None = None,
    technical_error: Exception | None = None,
    technical_failure_class: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Persist a raw response/usage record before any semantic apply path runs."""

    usage = _m3_v2_usage_payload(result)
    payload = {
        "phase": "L2A-M4d",
        "milestone": "M3_v2_request",
        "source": source,
        "scope": str(meta["scope"]),
        "chapter_id": str(meta["chapter_id"]),
        "mode": str(meta["mode"]),
        "shard_index": int(meta["shard_index"]),
        "shard_count": int(meta["shard_count"]),
        "attempt_index": int(meta["attempt_index"]),
        "tag": str(meta["tag"]),
        "prompt_sha256": canonical_hash(messages),
        "prompt_tokens_est": int(meta["prompt_tokens_est"]),
        "bypass_cache": bool(meta.get("bypass_cache", False)),
        "model": result.model if result is not None else None,
        "system_fingerprint": result.system_fingerprint if result is not None else None,
        "from_cache": bool(result.from_cache) if result is not None else False,
        "cache_key": result.cache_key if result is not None else None,
        "latency_ms": int(result.latency_ms) if result is not None else 0,
        "cost_usd": float(result.cost_usd) if result is not None else 0.0,
        "usage": usage,
        "json_error": result.json_error if result is not None else None,
        "raw_text": result.text if result is not None else None,
        "parsed_json": result.parsed_json if result is not None else provided_json,
        "technical_error": (
            {"type": type(technical_error).__name__, "message": str(technical_error)}
            if technical_error is not None
            else None
        ),
        "technical_failure_class": technical_failure_class,
    }
    for collision_index in range(10_000):
        path = _m3_v2_raw_response_path(
            out_dir,
            chapter_id=str(meta["chapter_id"]),
            mode=str(meta["mode"]),
            shard_index=int(meta["shard_index"]),
            attempt_index=int(meta["attempt_index"]),
            collision_index=collision_index,
        )
        try:
            _append_only_json_write(path, payload)
        except FileExistsError:
            continue
        break
    else:
        raise RuntimeError("M3 v2 could not allocate an append-only raw response path")
    checkpoint_record = {
        "mode": payload["mode"],
        "scope": payload["scope"],
        "shard_index": payload["shard_index"],
        "attempt_index": payload["attempt_index"],
        "source": source,
        "raw_response_path": path.relative_to(Path(out_dir)).as_posix(),
        "prompt_sha256": payload["prompt_sha256"],
        "model": payload["model"],
        "from_cache": payload["from_cache"],
        "cache_key": payload["cache_key"],
        "cost_usd": payload["cost_usd"],
        "usage": usage,
        "technical_failure_class": payload["technical_failure_class"],
    }
    return path, checkpoint_record


def _resolve_m3_v2_response_batch(
    *,
    supplied_scope_responses: dict[str, Any] | None,
    field: str,
    mode: str,
    shards: list[dict[str, Any]],
    out_dir: Path,
    scope: str,
    chapter_id: str,
    request_llm: RequestLLM | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Path], dict[str, int | float]]:
    """Use supplied synthetic JSON or request each runtime shard in the apply loop."""

    accounting = _empty_m3_v2_request_accounting()
    raw_records: list[dict[str, Any]] = []
    raw_paths: list[Path] = []
    if isinstance(supplied_scope_responses, dict) and field in supplied_scope_responses:
        responses = _response_batch(
            supplied_scope_responses,
            field=field,
            expected_count=len(shards),
        )
        for shard_index, (response, shard) in enumerate(zip(responses, shards, strict=True), start=1):
            meta = {
                "scope": scope,
                "chapter_id": chapter_id,
                "mode": mode,
                "shard_index": shard_index,
                "shard_count": len(shards),
                "attempt_index": 1,
                "tag": f"synthetic_m3v2_{chapter_id}_{mode}_s{shard_index:02d}",
                "prompt_tokens_est": int(shard["prompt_tokens_est"]),
                "bypass_cache": False,
            }
            raw_path, raw_record = _persist_m3_v2_raw_response(
                out_dir,
                messages=shard["messages"],
                meta=meta,
                source="provided_synthetic",
                provided_json=copy.deepcopy(response),
            )
            raw_paths.append(raw_path)
            raw_records.append(raw_record)
        return responses, raw_records, raw_paths, accounting

    if request_llm is None:
        raise M3V2SemanticGateError("scope_responses_missing", [scope, field])

    responses: list[dict[str, Any]] = []
    for shard_index, shard in enumerate(shards, start=1):
        accounting["logical_calls"] = int(accounting["logical_calls"]) + 1
        parsed: dict[str, Any] | None = None
        last_error: str | None = None
        for attempt_index in [1, 2]:
            poisoned_cache_replay = False
            meta = {
                "scope": scope,
                "chapter_id": chapter_id,
                "mode": mode,
                "shard_index": shard_index,
                "shard_count": len(shards),
                "attempt_index": attempt_index,
                "tag": f"lit_m3v2_{chapter_id}_{mode}_s{shard_index:02d}:attempt{attempt_index}",
                "prompt_tokens_est": int(shard["prompt_tokens_est"]),
                "bypass_cache": attempt_index > 1,
            }
            accounting["attempts"] = int(accounting["attempts"]) + 1
            try:
                result = request_llm(shard["messages"], meta)
            except Exception as exc:
                raw_path, raw_record = _persist_m3_v2_raw_response(
                    out_dir,
                    messages=shard["messages"],
                    meta=meta,
                    source="request_llm",
                    technical_error=exc,
                )
                raw_paths.append(raw_path)
                raw_records.append(raw_record)
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if not isinstance(result, LLMResult):
                    contract_error = TypeError(
                        "request_llm must return LLMResult from the shared cache-backed client"
                    )
                    raw_path, raw_record = _persist_m3_v2_raw_response(
                        out_dir,
                        messages=shard["messages"],
                        meta=meta,
                        source="request_llm",
                        technical_error=contract_error,
                    )
                    raw_paths.append(raw_path)
                    raw_records.append(raw_record)
                    last_error = str(contract_error)
                else:
                    parsed_json_ok = (
                        isinstance(result.parsed_json, dict) and result.json_error is None
                    )
                    poisoned_cache_replay = bool(result.from_cache and not parsed_json_ok)
                    raw_path, raw_record = _persist_m3_v2_raw_response(
                        out_dir,
                        messages=shard["messages"],
                        meta=meta,
                        source="request_llm",
                        result=result,
                        technical_failure_class=(
                            "poisoned_cache_replay" if poisoned_cache_replay else None
                        ),
                    )
                    raw_paths.append(raw_path)
                    raw_records.append(raw_record)
                    accounting["cache_hits"] = int(accounting["cache_hits"]) + int(result.from_cache)
                    accounting["cost_usd"] = round(
                        float(accounting["cost_usd"]) + float(result.cost_usd), 12
                    )
                    usage = _m3_v2_usage_payload(result)
                    for key, value in usage.items():
                        accounting[key] = int(accounting[key]) + int(value)
                    if parsed_json_ok:
                        parsed = result.parsed_json
                        break
                    last_error = result.json_error or "parsed_json_not_object"
                    if poisoned_cache_replay:
                        # Cached malformed JSON is free to replay and must not consume
                        # the fresh-spend retry budget. The forced bypass is still audited.
                        accounting["poisoned_cache_replays"] = (
                            int(accounting["poisoned_cache_replays"]) + 1
                        )

            if attempt_index == 1:
                if not poisoned_cache_replay:
                    accounting["technical_retries"] = int(accounting["technical_retries"]) + 1
                continue
            raise M3V2TechnicalGateError(
                "request_llm_parse_or_transport_failed",
                [scope, mode, f"shard={shard_index}", last_error or "unknown"],
                accounting=accounting,
            )
        if parsed is None:  # pragma: no cover - loop either breaks or raises.
            raise M3V2TechnicalGateError(
                "request_llm_missing_parsed_response",
                [scope, mode],
                accounting=accounting,
            )
        responses.append(parsed)
    return responses, raw_records, raw_paths, accounting


def _m3_v2_prefix(
    *,
    document: dict[str, Any],
    chain: dict[str, Any],
    out_dir: Path,
    design_doc: Path,
    config: LLMConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the valid M3 v2 prefix and resume mismatches, never globbing files."""

    checkpoints: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    parent_hash: str | None = None
    selected = chain["selected"]
    for index, chapter in enumerate(selected):
        chapter_id = str(chapter["chapter_id"])
        path = _m3_v2_checkpoint_path(out_dir, chapter_id)
        if not path.is_file():
            mismatches.append({"chapter_id": chapter_id, "fields": ["missing"]})
            break
        checkpoint = read_checkpoint(path)
        expected = {
            "stage": M3_V2_STAGE,
            "chapter_id": chapter_id,
            "chapter_index": index,
            "chapter_sequence_prefix": [str(item["chapter_id"]) for item in selected[: index + 1]],
            "source_hash": chapter_source_hash(chapter),
            "prompt_hashes": _m3_v2_prompt_hashes(design_doc, chapter_id),
            "config_hash": _m3_v2_config_hash(config),
            "schema_version": M3_V2_CHECKPOINT_SCHEMA_VERSION,
            "parent_checkpoint_hash": parent_hash,
            "input_m1_checkpoint_hash": str(chain["m1_checkpoints"][index]["checkpoint_hash"]),
            "input_m2_checkpoint_hash": str(chain["m2_checkpoints"][index]["checkpoint_hash"]),
        }
        errors = validate_checkpoint(checkpoint, root=out_dir, expected=expected)
        if errors:
            mismatches.append({"chapter_id": chapter_id, "fields": errors})
            break
        state = (checkpoint.get("state") or {}).get("m3_state")
        if not isinstance(state, dict):
            mismatches.append({"chapter_id": chapter_id, "fields": ["m3_state"]})
            break
        checkpoints.append(checkpoint)
        parent_hash = str(checkpoint["checkpoint_hash"])
    return checkpoints, mismatches


def rerender_m3_v2_from_checkpoints(
    document: dict[str, Any],
    chapters: list[str],
    *,
    out_dir: Path,
    design_doc: Path,
    config: LLMConfig,
    m1_dir: Path,
    m2_dir: Path,
) -> dict[str, Any]:
    """Rebuild published Story Bible views from an already-valid M3 checkpoint chain.

    This is deliberately not a resume path: it never prepares a model request,
    mutates consolidation state, or reads an API key.  It exists for mechanical
    output-contract migrations whose source of truth is the saved M3 state.
    """

    root = Path(out_dir)
    chain = load_m3_v2_input_chain(
        document=document,
        chapters=chapters,
        m1_dir=Path(m1_dir),
        m2_dir=Path(m2_dir),
        design_doc=Path(design_doc),
    )
    existing, mismatches = _m3_v2_prefix(
        document=document,
        chain=chain,
        out_dir=root,
        design_doc=Path(design_doc),
        config=config,
    )
    if mismatches or len(existing) != len(chain["selected"]):
        raise ValueError(
            "M3 v2 re-render requires a complete valid checkpoint prefix: "
            f"mismatches={mismatches}"
        )

    plans: list[dict[str, Any]] = []
    for index, (chapter, checkpoint) in enumerate(zip(chain["selected"], existing, strict=True)):
        checkpoint_state = copy.deepcopy(checkpoint.get("state") or {})
        state = _copy_state(checkpoint_state.get("m3_state") or {})
        if not state:
            raise ValueError(f"M3 v2 re-render checkpoint has no state: {chapter['chapter_id']}")
        turns = list(state.get("speaker_turns") or [])
        events = list(state.get("relation_events") or [])
        digests = _digest_payloads_as_of(
            m2_dir=Path(m2_dir),
            m2_checkpoints=chain["m2_checkpoints"][: index + 1],
        )
        story = build_story_bible_v2(
            chapter=chapter,
            state=state,
            m1_dir=Path(m1_dir),
            m1_checkpoints=chain["m1_checkpoints"][: index + 1],
            digests=digests,
        )
        errors = validate_story_bible_v2(
            story,
            expected_turn_count=len(turns),
            expected_event_count=len(events),
        )
        if errors:
            raise M3V2SemanticGateError("rerender_publish_gate_rejected", errors)
        story_path = root / "story_bible_v2" / f"{chapter['chapter_id']}_story_bible.json"
        raw_paths = _manifest_paths(
            root=root,
            checkpoints=[checkpoint],
            directory="raw_responses",
        )
        plans.append(
            {
                "chapter": chapter,
                "index": index,
                "checkpoint": checkpoint,
                "checkpoint_state": checkpoint_state,
                "story": story,
                "story_path": story_path,
                "raw_paths": raw_paths,
            }
        )

    lock = CheckpointLock(root)
    lock.acquire()
    scope_reports: list[dict[str, Any]] = []
    try:
        parent_hash: str | None = None
        for plan in plans:
            chapter = plan["chapter"]
            index = int(plan["index"])
            story_path = Path(plan["story_path"])
            _atomic_json_write(story_path, plan["story"])
            checkpoint = build_m3_v2_checkpoint(
                out_dir=root,
                chapter=chapter,
                chapter_index=index,
                chapter_sequence_prefix=[
                    str(item["chapter_id"]) for item in chain["selected"][: index + 1]
                ],
                design_doc=Path(design_doc),
                config=config,
                input_m1_checkpoint_hash=str(chain["m1_checkpoints"][index]["checkpoint_hash"]),
                input_m2_checkpoint_hash=str(chain["m2_checkpoints"][index]["checkpoint_hash"]),
                parent_checkpoint_hash=parent_hash,
                state=plan["checkpoint_state"],
                raw_responses=copy.deepcopy(plan["checkpoint"].get("raw_responses") or []),
                published_artifacts=[story_path, *plan["raw_paths"]],
            )
            checkpoint_path = write_m3_v2_checkpoint_atomic(root, checkpoint)
            parent_hash = str(checkpoint["checkpoint_hash"])
            scope_reports.append(
                {
                    "chapter_id": str(chapter["chapter_id"]),
                    "status": "rerendered",
                    "story_bible": str(story_path),
                    "checkpoint": str(checkpoint_path),
                    "previous_checkpoint_hash": str(plan["checkpoint"]["checkpoint_hash"]),
                    "checkpoint_hash": parent_hash,
                }
            )

        verified, verification_mismatches = _m3_v2_prefix(
            document=document,
            chain=chain,
            out_dir=root,
            design_doc=Path(design_doc),
            config=config,
        )
        if verification_mismatches or len(verified) != len(chain["selected"]):
            raise RuntimeError(
                "M3 v2 re-render wrote an invalid checkpoint chain: "
                f"mismatches={verification_mismatches}"
            )
    finally:
        lock.release()

    report = {
        "phase": "L2A-M4d",
        "milestone": "M3_v2_rerender",
        "zero_api": True,
        "status": "rerendered",
        "chapters_selected": [str(item["chapter_id"]) for item in chain["selected"]],
        "scopes": scope_reports,
        "resume": {
            "validated": [str(item["chapter_id"]) for item in verified],
            "mismatches": [],
            "lock_took_over_stale": lock.took_over_stale,
        },
        "stop": "Re-render complete. No LLM request was prepared or sent.",
    }
    _atomic_json_write(root / "m3_v2_rerender_report.json", report)
    return report


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_m3_v2_from_responses(
    document: dict[str, Any],
    chapters: list[str],
    *,
    out_dir: Path,
    design_doc: Path,
    config: LLMConfig,
    m1_dir: Path,
    m2_dir: Path,
    responses_by_scope: dict[str, dict[str, Any]] | None = None,
    request_llm: RequestLLM | None = None,
    confirm_usd: float | None = None,
    max_technical_retry_rate: float = M3_V2_MAX_TECHNICAL_RETRY_RATE,
    resume: bool = False,
) -> dict[str, Any]:
    """Apply supplied or in-loop model responses and publish only passed artifacts.

    ``request_llm`` is optional so the same executor remains usable for 0-API
    synthetic tests. When present, it receives the exact post-state runtime
    messages and must return the shared ``LLMResult`` shape. Its raw output and
    usage are persisted before semantic validation or state mutation.
    """

    if responses_by_scope is None and request_llm is None:
        raise ValueError("Provide synthetic responses_by_scope or a request_llm hook")
    if not 0.0 <= max_technical_retry_rate <= 1.0:
        raise ValueError("max_technical_retry_rate must be between 0 and 1")
    supplied_responses = dict(responses_by_scope or {})

    chain = load_m3_v2_input_chain(
        document=document,
        chapters=chapters,
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        design_doc=design_doc,
    )
    estimate: dict[str, Any] | None = None
    if request_llm is not None:
        estimate = estimate_m3_v2(
            document,
            chapters,
            design_doc=design_doc,
            config=config,
            m1_dir=m1_dir,
            m2_dir=m2_dir,
        )
        if confirm_usd is None:
            raise ValueError("--confirm-usd is required when M3 v2 supplies request_llm")
        if float(estimate["cost_cap_usd"]) > float(confirm_usd):
            raise ValueError(
                "M3 v2 refused: estimate cost cap "
                f"${float(estimate['cost_cap_usd']):.4f} exceeds --confirm-usd ${float(confirm_usd):.4f}"
            )
    scopes = _scope_payloads(
        document=document,
        chain=chain,
        m1_dir=Path(m1_dir),
        m2_dir=Path(m2_dir),
        design_doc=Path(design_doc),
        config=config,
    )
    source_text = _source_text_by_block(document)
    ordinals = {
        str(block.get("block_id") or ""): int(block.get("order_index") or 0)
        for chapter in document.get("chapters") or []
        for block in chapter.get("blocks") or []
    }
    report_path = Path(out_dir) / "m3_v2_report.json"
    lock = CheckpointLock(Path(out_dir))
    lock.acquire()
    restored_request_accounting = _empty_m3_v2_request_accounting()
    this_attempt_request_accounting = _empty_m3_v2_request_accounting()
    scope_reports: list[dict[str, Any]] = []
    try:
        restored: list[dict[str, Any]] = []
        resume_mismatches: list[dict[str, Any]] = []
        state = empty_m3_v2_state()
        parent_hash: str | None = None
        if resume:
            restored, resume_mismatches = _m3_v2_prefix(
                document=document,
                chain=chain,
                out_dir=Path(out_dir),
                design_doc=design_doc,
                config=config,
            )
            if restored:
                state = _copy_state((restored[-1].get("state") or {}).get("m3_state") or {})
                parent_hash = str(restored[-1]["checkpoint_hash"])
            for checkpoint in restored:
                checkpoint_state = checkpoint.get("state") or {}
                restored_request_accounting = _add_m3_v2_request_accounting(
                    restored_request_accounting,
                    checkpoint_state.get("request_accounting") or {},
                )
        start_index = len(restored)
        for index, scope in enumerate(scopes[start_index:], start=start_index):
            chapter = chain["selected"][index]
            chapter_id = str(chapter["chapter_id"])
            scope_key = str(scope["scope"])
            scope_responses = supplied_responses.get(scope_key)

            identity_shards = _runtime_identity_shards(
                frontier_atoms=scope["frontier_atoms"],
                state=state,
                design_doc=Path(design_doc),
                chapter_id=chapter_id,
                scope=scope_key,
                identity_hints=scope["identity_hints"],
                prompt_cap=config.prompt_token_cap,
            )
            if any("dry_run_note" in shard["messages"][1]["content"] for shard in identity_shards):
                raise RuntimeError("M3 v2 runtime identity request retained dry_run_note")
            (
                identity_responses,
                identity_raw_records,
                identity_raw_paths,
                identity_request_accounting,
            ) = _resolve_m3_v2_response_batch(
                supplied_scope_responses=scope_responses,
                field="identity",
                mode=IDENTITY_PARTITION_VERSION,
                shards=identity_shards,
                out_dir=Path(out_dir),
                scope=scope_key,
                chapter_id=chapter_id,
                request_llm=request_llm,
            )
            this_attempt_request_accounting = _add_m3_v2_request_accounting(
                this_attempt_request_accounting,
                identity_request_accounting,
            )
            identity_responses, identity_shard_audit = _normalize_identity_responses_by_shard(
                identity_responses,
                identity_shards=identity_shards,
            )
            identity_payload = {
                "groups": [
                    group
                    for response in identity_responses
                    for group in response.get("groups") or []
                ]
            }
            state, identity_audit = apply_identity_partition_response(
                state,
                identity_payload,
                atoms=scope["frontier_atoms"],
                source_text_by_block=source_text,
            )
            for key, value in identity_shard_audit.items():
                identity_audit[key] = int(identity_audit.get(key, 0)) + int(value)

            (
                mapped_phase_rows,
                unresolved_phase_rows,
                provisional_binding_audit,
            ) = _remap_phase_rows_to_final_ids(state, scope["phase_rows"])
            if unresolved_phase_rows:
                raise M3V2SemanticGateError(
                    "blocked_for_runtime_unresolved_relation",
                    [str(row.get("reason") or "") for row in unresolved_phase_rows],
                )
            phase_batches, phase_input_audit = _merge_mapped_phase_batches(
                phase_rows=mapped_phase_rows
            )
            allowed_pairs = {tuple(row["pair"]) for row in phase_batches}
            phase_shards = _runtime_phase_shards(
                phase_batches=phase_batches,
                design_doc=Path(design_doc),
                chapter_id=chapter_id,
                scope=scope_key,
                prompt_cap=config.prompt_token_cap,
            )
            if any("dry_run_note" in shard["messages"][1]["content"] for shard in phase_shards):
                raise RuntimeError("M3 v2 runtime phase request retained dry_run_note")
            (
                phase_responses,
                phase_raw_records,
                phase_raw_paths,
                phase_request_accounting,
            ) = _resolve_m3_v2_response_batch(
                supplied_scope_responses=scope_responses,
                field="phase",
                mode=PHASE_SEGMENT_VERSION,
                shards=phase_shards,
                out_dir=Path(out_dir),
                scope=scope_key,
                chapter_id=chapter_id,
                request_llm=request_llm,
            )
            this_attempt_request_accounting = _add_m3_v2_request_accounting(
                this_attempt_request_accounting,
                phase_request_accounting,
            )
            phase_payload = {
                "relation_facts": [
                    row
                    for response in phase_responses
                    for row in response.get("relation_facts") or []
                ],
                "relation_phases": [
                    row
                    for response in phase_responses
                    for row in response.get("relation_phases") or []
                ],
            }
            state, phase_model_audit = apply_phase_segment_response(
                state,
                phase_payload,
                allowed_pairs=allowed_pairs,
                source_text_by_block=source_text,
                block_ordinals=ordinals,
                scope_end_block=(str(chapter["blocks"][-1]["block_id"]) if chapter.get("blocks") else None),
            )
            phase_audit = {
                **provisional_binding_audit,
                **phase_input_audit,
                **phase_model_audit,
            }

            combined_request_accounting = _add_m3_v2_request_accounting(
                restored_request_accounting,
                this_attempt_request_accounting,
            )
            technical_retry_rate = (
                float(combined_request_accounting["technical_retries"])
                / float(combined_request_accounting["logical_calls"])
                if int(combined_request_accounting["logical_calls"])
                else 0.0
            )
            if request_llm is not None and technical_retry_rate > max_technical_retry_rate:
                raise M3V2TechnicalGateError(
                    "technical_retry_rate_exceeded",
                    [
                        f"retries={int(combined_request_accounting['technical_retries'])}",
                        f"logical_calls={int(combined_request_accounting['logical_calls'])}",
                        f"rate={technical_retry_rate:.4f}",
                        f"max={max_technical_retry_rate:.4f}",
                    ],
                )

            turns, events = _asof_interactions(
                m1_dir=Path(m1_dir),
                m1_checkpoints=chain["m1_checkpoints"][: index + 1],
                state=state,
            )
            state["speaker_turns"] = turns
            state["relation_events"] = events
            digests = _digest_payloads_as_of(
                m2_dir=Path(m2_dir),
                m2_checkpoints=chain["m2_checkpoints"][: index + 1],
            )
            story = build_story_bible_v2(
                chapter=chapter,
                state=state,
                m1_dir=Path(m1_dir),
                m1_checkpoints=chain["m1_checkpoints"][: index + 1],
                digests=digests,
            )
            gate_errors = validate_story_bible_v2(
                story,
                expected_turn_count=len(turns),
                expected_event_count=len(events),
            )
            if gate_errors:
                raise M3V2SemanticGateError("publish_gate_rejected", gate_errors)

            story_path = Path(out_dir) / "story_bible_v2" / f"{chapter_id}_story_bible.json"
            _atomic_json_write(story_path, story)
            raw_responses = [*identity_raw_records, *phase_raw_records]
            scope_request_accounting = _add_m3_v2_request_accounting(
                identity_request_accounting,
                phase_request_accounting,
            )
            checkpoint = build_m3_v2_checkpoint(
                out_dir=Path(out_dir),
                chapter=chapter,
                chapter_index=index,
                chapter_sequence_prefix=[
                    str(item["chapter_id"]) for item in chain["selected"][: index + 1]
                ],
                design_doc=Path(design_doc),
                config=config,
                input_m1_checkpoint_hash=scope["m1_checkpoint_hash"],
                input_m2_checkpoint_hash=scope["m2_checkpoint_hash"],
                parent_checkpoint_hash=parent_hash,
                state={
                    "m3_state": state,
                    "identity_audit": identity_audit,
                    "phase_audit": phase_audit,
                    "request_accounting": scope_request_accounting,
                },
                raw_responses=raw_responses,
                published_artifacts=[story_path, *identity_raw_paths, *phase_raw_paths],
            )
            checkpoint_path = write_m3_v2_checkpoint_atomic(Path(out_dir), checkpoint)
            parent_hash = str(checkpoint["checkpoint_hash"])
            scope_reports.append(
                {
                    "scope": scope_key,
                    "chapter_id": chapter_id,
                    "status": "published",
                    "identity_audit": identity_audit,
                    "phase_audit": phase_audit,
                    "runtime_request_plan": {
                        "identity_shards": len(identity_shards),
                        "phase_shards": len(phase_shards),
                        "dry_run_note_omitted": True,
                    },
                    "request_accounting": scope_request_accounting,
                    "story_bible": str(story_path),
                    "checkpoint": str(checkpoint_path),
                }
            )
        report = {
            "phase": "L2A-M4d",
            "milestone": "M3_v2_apply",
            "zero_api": request_llm is None,
            "status": "needs_claude_gate",
            "estimate": estimate,
            "request_accounting": {
                "restored": restored_request_accounting,
                "this_attempt": this_attempt_request_accounting,
                "combined": _add_m3_v2_request_accounting(
                    restored_request_accounting,
                    this_attempt_request_accounting,
                ),
            },
            "chapters_selected": [str(item["chapter_id"]) for item in chain["selected"]],
            "resume": {
                "enabled": resume,
                "restored": [str(item["chapter_id"]) for item in restored],
                "mismatches": resume_mismatches,
                "lock_took_over_stale": lock.took_over_stale,
            },
            "scopes": scope_reports,
            "stop": (
                "Apply/publish complete. Claude must approve artifacts before any further API run."
            ),
        }
    except M3V2SemanticGateError as exc:
        report = {
            "phase": "L2A-M4d",
            "milestone": "M3_v2_apply",
            "zero_api": request_llm is None,
            "status": "halted_semantic_gate",
            "gate_code": exc.code,
            "errors": exc.errors,
            "estimate": estimate,
            "request_accounting": {
                "restored": restored_request_accounting,
                "this_attempt": this_attempt_request_accounting,
                "combined": _add_m3_v2_request_accounting(
                    restored_request_accounting,
                    this_attempt_request_accounting,
                ),
            },
            "stop": "No failed scope was published or checkpointed. Resolve the semantic gate before API.",
        }
    except M3V2TechnicalGateError as exc:
        this_attempt_request_accounting = _add_m3_v2_request_accounting(
            this_attempt_request_accounting,
            exc.accounting,
        )
        report = {
            "phase": "L2A-M4d",
            "milestone": "M3_v2_apply",
            "zero_api": request_llm is None,
            "status": "halted_technical_gate",
            "gate_code": exc.code,
            "errors": exc.errors,
            "estimate": estimate,
            "request_accounting": {
                "restored": restored_request_accounting,
                "this_attempt": this_attempt_request_accounting,
                "combined": _add_m3_v2_request_accounting(
                    restored_request_accounting,
                    this_attempt_request_accounting,
                ),
            },
            "stop": "Raw attempts were retained; resolve the technical gate before retrying.",
        }
    finally:
        lock.release()
    _atomic_json_write(report_path, report)
    return report


def _source_text_by_block(document: dict[str, Any]) -> dict[str, str]:
    by_id, _chapter_for_block = _block_maps(document)
    return {
        block_id: str(block.get("clean_text") or block.get("source_text") or "")
        for block_id, block in by_id.items()
    }


_QUOTE_PUNCTUATION_FOLD = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
})


def _fold_quote_punctuation(value: str) -> str:
    """Normalize only punctuation variants that do not alter lexical content."""

    return str(value).translate(_QUOTE_PUNCTUATION_FOLD)


def _quote_offsets(text: str, quote: str) -> list[int]:
    offsets: list[int] = []
    start = 0
    while True:
        offset = text.find(quote, start)
        if offset < 0:
            return offsets
        offsets.append(offset)
        start = offset + 1


def _locate_quote_in_blocks(
    quote: str,
    *,
    source_text_by_block: dict[str, str],
    candidate_block_ids: Iterable[str],
    require_unique_exact: bool,
) -> tuple[str, str, bool] | None:
    """Locate source text and only normalize punctuation after exact matching fails."""

    if not quote:
        return None
    block_ids = list(
        dict.fromkeys(
            str(block_id)
            for block_id in candidate_block_ids
            if str(block_id) in source_text_by_block
        )
    )
    exact_matches = [
        (block_id, offset)
        for block_id in block_ids
        for offset in _quote_offsets(str(source_text_by_block[block_id]), quote)
    ]
    if exact_matches:
        if require_unique_exact and len(exact_matches) != 1:
            return None
        block_id, offset = exact_matches[0]
        return block_id, str(source_text_by_block[block_id])[offset : offset + len(quote)], False

    folded_quote = _fold_quote_punctuation(quote)
    folded_matches = [
        (block_id, offset)
        for block_id in block_ids
        for offset in _quote_offsets(
            _fold_quote_punctuation(str(source_text_by_block[block_id])),
            folded_quote,
        )
    ]
    if len(folded_matches) != 1:
        return None
    block_id, offset = folded_matches[0]
    source = str(source_text_by_block[block_id])
    return block_id, source[offset : offset + len(quote)], True


def _default_scope_end_block(block_ordinals: dict[str, int]) -> str:
    if not block_ordinals:
        return ""
    return max(block_ordinals, key=lambda block_id: (block_ordinals[block_id], block_id))


def _phase_range_block_ids(
    *,
    start_block: str,
    valid_until_block: Any,
    scope_end_block: str,
    block_ordinals: dict[str, int],
    source_text_by_block: dict[str, str],
) -> list[str]:
    if start_block not in block_ordinals:
        return []
    end_block = str(valid_until_block) if valid_until_block is not None else scope_end_block
    if end_block not in block_ordinals:
        return []
    start_ordinal = block_ordinals[start_block]
    end_ordinal = block_ordinals[end_block]
    if end_ordinal < start_ordinal:
        return []
    return [
        block_id
        for block_id, _ordinal in sorted(block_ordinals.items(), key=lambda item: (item[1], item[0]))
        if block_id in source_text_by_block and start_ordinal <= _ordinal <= end_ordinal
    ]


def _normalize_identity_evidence_quotes(
    response: dict[str, Any],
    *,
    source_text_by_block: dict[str, str],
    quote_audit: dict[str, int],
) -> None:
    for group in response.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for evidence in group.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            block_id = str(evidence.get("block_id") or "")
            located = _locate_quote_in_blocks(
                str(evidence.get("quote") or ""),
                source_text_by_block=source_text_by_block,
                candidate_block_ids=[block_id],
                require_unique_exact=False,
            )
            if located is None:
                continue
            _located_block, source_quote, punct_normalized = located
            evidence["quote"] = source_quote
            if punct_normalized:
                quote_audit["evidence_quote_punct_normalized"] += 1


def _normalize_phase_evidence_quotes(
    response: dict[str, Any],
    *,
    source_text_by_block: dict[str, str],
    block_ordinals: dict[str, int],
    scope_end_block: str,
    quote_audit: dict[str, int],
) -> None:
    for phase in response.get("relation_phases") or []:
        if not isinstance(phase, dict):
            continue
        phase.pop("trigger_evidence_block", None)
        candidate_block_ids = _phase_range_block_ids(
            start_block=str(phase.get("valid_from_block") or ""),
            valid_until_block=phase.get("valid_until_block"),
            scope_end_block=scope_end_block,
            block_ordinals=block_ordinals,
            source_text_by_block=source_text_by_block,
        )
        quote = str(phase.get("trigger_evidence") or "")
        trigger_block = str(phase.get("trigger_block") or "")
        trigger_source = str(source_text_by_block.get(trigger_block) or "")
        if quote and quote in trigger_source:
            offset = trigger_source.find(quote)
            located = (trigger_block, trigger_source[offset : offset + len(quote)], False)
        else:
            located = _locate_quote_in_blocks(
                quote,
                source_text_by_block=source_text_by_block,
                candidate_block_ids=candidate_block_ids,
                require_unique_exact=True,
            )
        if located is None:
            continue
        located_block, source_quote, punct_normalized = located
        phase["trigger_evidence"] = source_quote
        phase["trigger_evidence_block"] = located_block
        if punct_normalized:
            quote_audit["evidence_quote_punct_normalized"] += 1

    for fact in response.get("relation_facts") or []:
        if not isinstance(fact, dict):
            continue
        block_id = str(fact.get("evidence_block") or "")
        located = _locate_quote_in_blocks(
            str(fact.get("evidence_quote") or ""),
            source_text_by_block=source_text_by_block,
            candidate_block_ids=[block_id],
            require_unique_exact=False,
        )
        if located is None:
            continue
        _located_block, source_quote, punct_normalized = located
        fact["evidence_quote"] = source_quote
        if punct_normalized:
            quote_audit["evidence_quote_punct_normalized"] += 1


def validate_identity_partition_response(
    response: dict[str, Any],
    *,
    atoms: list[dict[str, Any]],
    prior_entity_ids: set[str],
    source_text_by_block: dict[str, str],
    quote_audit: dict[str, int] | None = None,
) -> list[str]:
    """Validate the identity response without applying any linguistic decision."""

    errors: list[str] = []
    if not isinstance(response, dict):
        return ["groups must be a list"]
    audit = quote_audit if quote_audit is not None else {"evidence_quote_punct_normalized": 0}
    audit.setdefault("evidence_quote_punct_normalized", 0)
    _normalize_identity_evidence_quotes(response, source_text_by_block=source_text_by_block, quote_audit=audit)
    groups = response.get("groups")
    if not isinstance(groups, list):
        return ["groups must be a list"]
    atom_ids = {str(atom["atom_id"]) for atom in atoms}
    seen_atoms: set[str] = set()
    seen_group_keys: set[str] = set()
    for index, group in enumerate(groups):
        prefix = f"groups[{index}]"
        if not isinstance(group, dict):
            errors.append(f"{prefix} must be object")
            continue
        group_key = str(group.get("group_key") or "")
        if not group_key or group_key in seen_group_keys:
            errors.append(f"{prefix}.group_key missing_or_duplicate")
        seen_group_keys.add(group_key)
        members = [str(value) for value in group.get("member_atom_ids") or [] if str(value)]
        if not members:
            errors.append(f"{prefix}.member_atom_ids empty")
        for atom_id in members:
            if atom_id not in atom_ids:
                errors.append(f"{prefix}.member_atom_ids unknown:{atom_id}")
            elif atom_id in seen_atoms:
                errors.append(f"{prefix}.member_atom_ids duplicate:{atom_id}")
            seen_atoms.add(atom_id)
        canonical = str(group.get("canonical_atom_id") or "")
        if canonical not in members:
            errors.append(f"{prefix}.canonical_atom_id not_member")
        kind = str(group.get("referent_kind") or "")
        if kind not in IDENTITY_REFERENT_KINDS:
            errors.append(f"{prefix}.referent_kind invalid:{kind}")
        status = str(group.get("status") or "")
        if status not in IDENTITY_GROUP_STATUSES:
            errors.append(f"{prefix}.status invalid:{status}")
        reuse = group.get("reuse_entity_id")
        if reuse is not None and str(reuse) not in prior_entity_ids:
            errors.append(f"{prefix}.reuse_entity_id unknown:{reuse}")
        aliases = group.get("alias_bindings")
        if not isinstance(aliases, list):
            errors.append(f"{prefix}.alias_bindings must be list")
        else:
            for alias_index, alias in enumerate(aliases):
                alias_prefix = f"{prefix}.alias_bindings[{alias_index}]"
                if not isinstance(alias, dict):
                    errors.append(f"{alias_prefix} must be object")
                    continue
                if not str(alias.get("surface") or "").strip():
                    errors.append(f"{alias_prefix}.surface missing")
                alias_members = [str(value) for value in alias.get("member_atom_ids") or [] if str(value)]
                if not alias_members or any(item not in members for item in alias_members):
                    errors.append(f"{alias_prefix}.member_atom_ids invalid")
                start = str(alias.get("valid_from_block") or "")
                if start not in source_text_by_block:
                    errors.append(f"{alias_prefix}.valid_from_block invalid:{start}")
                end = alias.get("valid_until_block")
                if end is not None and str(end) not in source_text_by_block:
                    errors.append(f"{alias_prefix}.valid_until_block invalid:{end}")
        evidence = group.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{prefix}.evidence must be list")
        elif len(members) > 1 and not evidence:
            errors.append(f"{prefix}.evidence required_for_multi_atom_group")
        for evidence_index, row in enumerate(evidence or []):
            evidence_prefix = f"{prefix}.evidence[{evidence_index}]"
            if not isinstance(row, dict):
                errors.append(f"{evidence_prefix} must be object")
                continue
            block_id = str(row.get("block_id") or "")
            quote = str(row.get("quote") or "")
            if not block_id or block_id not in source_text_by_block:
                errors.append(f"{evidence_prefix}.block_id invalid:{block_id}")
            elif not quote or quote not in source_text_by_block[block_id]:
                errors.append(f"{evidence_prefix}.quote not_source_substring")
            source_atoms = [str(value) for value in row.get("source_atom_ids") or [] if str(value)]
            if not source_atoms or any(item not in atom_ids for item in source_atoms):
                errors.append(f"{evidence_prefix}.source_atom_ids invalid")
            elif not set(source_atoms) & set(members) and not (
                set(source_atoms)
                & {
                    str(value)
                    for value in group.get("_jurisdiction_stripped_member_atom_ids") or []
                    if str(value)
                }
            ):
                errors.append(f"{evidence_prefix}.source_atom_ids must_touch_group")
            if row.get("supports") not in {"same_identity", "different_identity"}:
                errors.append(f"{evidence_prefix}.supports invalid")
    if seen_atoms != atom_ids:
        errors.append("exact_partition mismatch")
    return errors


def validate_phase_segment_response(
    response: dict[str, Any],
    *,
    entity_ids: set[str],
    source_text_by_block: dict[str, str],
    block_ordinals: dict[str, int],
    allowed_pairs: set[tuple[str, str]] | None = None,
    scope_end_block: str | None = None,
    quote_audit: dict[str, int] | None = None,
) -> list[str]:
    """Validate batch phase/fact output before a future apply-to-copy gate."""

    errors: list[str] = []
    if not isinstance(response, dict):
        return ["response must be object"]
    audit = quote_audit if quote_audit is not None else {"evidence_quote_punct_normalized": 0}
    audit.setdefault("evidence_quote_punct_normalized", 0)
    effective_scope_end_block = scope_end_block or _default_scope_end_block(block_ordinals)
    if effective_scope_end_block not in block_ordinals:
        errors.append("scope_end_block invalid")
    _normalize_phase_evidence_quotes(
        response,
        source_text_by_block=source_text_by_block,
        block_ordinals=block_ordinals,
        scope_end_block=effective_scope_end_block,
        quote_audit=audit,
    )
    phases = response.get("relation_phases")
    facts = response.get("relation_facts")
    if not isinstance(phases, list):
        errors.append("relation_phases must be list")
        phases = []
    if not isinstance(facts, list):
        errors.append("relation_facts must be list")
        facts = []
    last_start_by_pair: dict[tuple[str, str], int] = {}
    last_end_by_pair: dict[tuple[str, str], int | None] = {}
    open_count_by_pair: dict[tuple[str, str], int] = {}
    for index, phase in enumerate(phases):
        prefix = f"relation_phases[{index}]"
        if not isinstance(phase, dict):
            errors.append(f"{prefix} must be object")
            continue
        pair = [str(item) for item in phase.get("pair") or [] if str(item)]
        if len(pair) != 2 or pair[0] == pair[1] or any(item not in entity_ids for item in pair):
            errors.append(f"{prefix}.pair invalid")
            continue
        pair_key = tuple(sorted(pair))
        if allowed_pairs is not None and pair_key not in allowed_pairs:
            errors.append(f"{prefix}.pair not_in_input_batch")
        if phase.get("phase_label") not in PHASE_LABELS:
            errors.append(f"{prefix}.phase_label invalid")
        start = str(phase.get("valid_from_block") or "")
        end = phase.get("valid_until_block")
        trigger_block = str(phase.get("trigger_block") or "")
        quote = str(phase.get("trigger_evidence") or "")
        if start not in block_ordinals or trigger_block not in block_ordinals:
            errors.append(f"{prefix}.start_or_trigger invalid")
            continue
        if block_ordinals[trigger_block] != block_ordinals[start]:
            errors.append(f"{prefix}.trigger_block must_equal_start")
        start_ordinal = block_ordinals[start]
        if start_ordinal <= last_start_by_pair.get(pair_key, -1):
            errors.append(f"{prefix}.phase order invalid")
        prior_end = last_end_by_pair.get(pair_key)
        if prior_end is None and pair_key in last_end_by_pair:
            errors.append(f"{prefix}.phase follows open interval")
        elif prior_end is not None and start_ordinal < prior_end:
            errors.append(f"{prefix}.phase intervals overlap")
        last_start_by_pair[pair_key] = start_ordinal
        if end is None:
            open_count_by_pair[pair_key] = open_count_by_pair.get(pair_key, 0) + 1
            if phase.get("status") != "open":
                errors.append(f"{prefix}.open phase requires status open")
            last_end_by_pair[pair_key] = None
        elif str(end) not in block_ordinals or block_ordinals[str(end)] <= start_ordinal:
            errors.append(f"{prefix}.valid_until_block invalid")
        else:
            last_end_by_pair[pair_key] = block_ordinals[str(end)]
        range_block_ids = _phase_range_block_ids(
            start_block=start,
            valid_until_block=end,
            scope_end_block=effective_scope_end_block,
            block_ordinals=block_ordinals,
            source_text_by_block=source_text_by_block,
        )
        trigger_evidence_block = str(phase.get("trigger_evidence_block") or "")
        if (
            not quote
            or trigger_evidence_block not in range_block_ids
            or quote not in source_text_by_block.get(trigger_evidence_block, "")
        ):
            errors.append(f"{prefix}.trigger_evidence not_source_substring")
    for pair, open_count in open_count_by_pair.items():
        if open_count > 1:
            errors.append(f"relation_phases open_interval duplicate:{pair}")
    for index, fact in enumerate(facts):
        prefix = f"relation_facts[{index}]"
        if not isinstance(fact, dict):
            errors.append(f"{prefix} must be object")
            continue
        subject = str(fact.get("subject_ref") or "")
        obj = str(fact.get("object_ref") or "")
        if not subject or not obj or subject == obj or subject not in entity_ids or obj not in entity_ids:
            errors.append(f"{prefix}.subject_or_object invalid")
        elif allowed_pairs is not None and tuple(sorted([subject, obj])) not in allowed_pairs:
            errors.append(f"{prefix}.subject_or_object not_in_input_batch")
        if fact.get("predicate_code") not in PREDICATE_CODES:
            errors.append(f"{prefix}.predicate_code invalid")
        valid_from = str(fact.get("valid_from_block") or "")
        if valid_from not in block_ordinals:
            errors.append(f"{prefix}.valid_from_block invalid")
        block_id = str(fact.get("evidence_block") or "")
        quote = str(fact.get("evidence_quote") or "")
        if block_id not in source_text_by_block or not quote or quote not in source_text_by_block.get(block_id, ""):
            errors.append(f"{prefix}.evidence not_source_substring")
    return errors


def run_m3_v2_dry_run(
    document: dict[str, Any],
    chapters: list[str],
    *,
    out_dir: Path,
    design_doc: Path,
    config: LLMConfig,
    m1_dir: Path,
    m2_dir: Path,
) -> dict[str, Any]:
    """Write reviewable B4 v2 prompts/estimate without calling a model or publishing state."""

    chain = load_m3_v2_input_chain(
        document=document,
        chapters=chapters,
        m1_dir=m1_dir,
        m2_dir=m2_dir,
        design_doc=design_doc,
    )
    scopes = _scope_payloads(
        document=document,
        chain=chain,
        m1_dir=Path(m1_dir),
        m2_dir=Path(m2_dir),
        design_doc=Path(design_doc),
        config=config,
    )
    estimate = estimate_m3_v2(
        document,
        chapters,
        design_doc=design_doc,
        config=config,
        m1_dir=m1_dir,
        m2_dir=m2_dir,
    )
    root = Path(out_dir) / "m3_v2_scaffold"
    root.mkdir(parents=True, exist_ok=True)
    rendered: list[dict[str, Any]] = []
    checkpoint_plans: list[dict[str, Any]] = []
    for index, scope in enumerate(scopes):
        scope_dir = root / scope["chapter_id"]
        scope_dir.mkdir(parents=True, exist_ok=True)
        for mode, shards in [
            (IDENTITY_PARTITION_VERSION, scope["identity_shards"]),
            (PHASE_SEGMENT_VERSION, scope["phase_shards"]),
        ]:
            for shard_index, shard in enumerate(shards, start=1):
                path = scope_dir / f"{mode}_shard_{shard_index:02d}.json"
                payload = {
                    "zero_api": True,
                    "scope": scope["scope"],
                    "chapter_id": scope["chapter_id"],
                    "mode": mode,
                    "shard_index": shard_index,
                    "shard_count": len(shards),
                    "items": shard["items"],
                    "prompt_tokens_est": shard["prompt_tokens_est"],
                    "messages": shard["messages"],
                }
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                rendered.append({"mode": mode, "scope": scope["scope"], "path": str(path)})
        checkpoint_plans.append(
            {
                "stage": M3_V2_STAGE,
                "chapter_id": scope["chapter_id"],
                "schema_version": M3_V2_CHECKPOINT_SCHEMA_VERSION,
                "input_m1_checkpoint_hash": scope["m1_checkpoint_hash"],
                "input_m2_checkpoint_hash": scope["m2_checkpoint_hash"],
                "parent_checkpoint_hash": "assigned_after_prior_scope_passes",
                "publish_rule": "atomic_after_all_semantic_gates_pass",
            }
        )
    final_scope = scopes[-1] if scopes else None
    final_frontier = list((final_scope or {}).get("frontier_atoms") or [])
    master_blocks = sorted(
        atom["block_id"]
        for atom in final_frontier
        if str(atom.get("surface") or "").casefold() == "the master"
    )
    young_master_blocks = sorted(
        atom["block_id"]
        for atom in final_frontier
        if str(atom.get("surface") or "").casefold() == "the young master"
    )
    gate_battery = {
        "status": "dry_run_input_ready_only",
        "checks": {
            "m1_m2_chain_validated": True,
            "as_of_atoms_only": all(
                all(atom["chapter_id"] in scope["as_of_chapters"] for atom in scope["frontier_atoms"])
                for scope in scopes
            ),
            "the_master_ch04_input": {
                "status": "ready_for_identity_gate" if master_blocks else "not_applicable_or_missing",
                "surface": "the master",
                "blocks": master_blocks,
                "required_distinct_groups_after_api": 3,
            },
            "the_young_master_ch04_input": {
                "status": "ready_for_identity_gate" if young_master_blocks else "not_applicable_or_missing",
                "surface": "the young master",
                "blocks": young_master_blocks,
            },
            "stable_id_ch01_to_ch04": "awaits identity responses; not evaluated in zero-API scaffold",
            "semantic_gate": "awaits API responses; no synthetic verdict was applied",
        },
    }
    report = {
        "phase": "L2A-M4d",
        "milestone": "M3_v2_scaffold",
        "status": "needs_claude_prompt_and_estimate_gate",
        "zero_api": True,
        "estimate": estimate,
        "m1_dir": str(m1_dir),
        "m2_dir": str(m2_dir),
        "rendered_prompts": rendered,
        "m3_checkpoint_plans": checkpoint_plans,
        "gate_battery": gate_battery,
        "input_chain": {
            "m1": [str(item["checkpoint_hash"]) for item in chain["m1_checkpoints"]],
            "m2": [str(item["checkpoint_hash"]) for item in chain["m2_checkpoints"]],
        },
        "stop": (
            "Scaffold complete. No LLM call, M3 checkpoint, or Story Bible was published. "
            "Claude must approve rendered prompts and the estimator before API execution."
        ),
    }
    report_path = root / "m3_v2_scaffold_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
