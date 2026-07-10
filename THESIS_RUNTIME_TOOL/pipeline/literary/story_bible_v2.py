from __future__ import annotations

"""Zero-API scaffold for the B4 Story Bible v2 contract.

This module deliberately does not reuse the pilot M3 resolver.  The pilot makes
book-specific-looking identity decisions from surface keys; B4 v2 only prepares
evidence atoms and validates model verdicts.  The actual API execution is kept
behind a later gate.  This file therefore makes the data contract, estimate,
checkpoint contract, and prompt rendering reviewable before any model call.
"""

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from pipeline.agents.llm_client import estimate_prompt_tokens
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
M3_V2_CHECKPOINT_SCHEMA_VERSION = "literary_m3_v2_checkpoint_v1"
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
    user_payload = {
        "scope": scope,
        "atoms": atoms,
        "prior_groups": prompt_prior_groups,
        "identity_hints": identity_hints,
        "dry_run_note": (
            "This scaffold uses provisional prior groups only for token sizing. "
            "They are not identity verdicts and will not be published."
        ),
    }
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
            rows.append(
                {
                    "source_chapter_id": chapter_id,
                    "provisional_pair": sorted(pair),
                    "event_ids": event_ids,
                    "events": [event_index[event_id] for event_id in event_ids if event_id in event_index],
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
) -> list[dict[str, str]]:
    user_payload = {
        "scope": scope,
        "predicate_taxonomy_version": PREDICATE_TAXONOMY_VERSION,
        "pair_evidence": phase_rows,
        "response_envelope": {
            "relation_facts": "list",
            "relation_phases": "list; every phase must include its pair because this is a batch",
        },
        "dry_run_note": (
            "Pair ids are pre-identity provisional ids for prompt sizing only. "
            "A real run remaps evidence to final ids after identity partition before this call."
        ),
    }
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


def _source_text_by_block(document: dict[str, Any]) -> dict[str, str]:
    by_id, _chapter_for_block = _block_maps(document)
    return {
        block_id: str(block.get("clean_text") or block.get("source_text") or "")
        for block_id, block in by_id.items()
    }


def validate_identity_partition_response(
    response: dict[str, Any],
    *,
    atoms: list[dict[str, Any]],
    prior_entity_ids: set[str],
    source_text_by_block: dict[str, str],
) -> list[str]:
    """Validate the identity response without applying any linguistic decision."""

    errors: list[str] = []
    groups = response.get("groups") if isinstance(response, dict) else None
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
            elif not set(source_atoms) & set(members):
                errors.append(f"{evidence_prefix}.source_atom_ids must_touch_group")
            elif row.get("supports") == "same_identity" and any(
                item not in members for item in source_atoms
            ):
                errors.append(f"{evidence_prefix}.same_identity source_atom outside_group")
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
) -> list[str]:
    """Validate batch phase/fact output before a future apply-to-copy gate."""

    errors: list[str] = []
    if not isinstance(response, dict):
        return ["response must be object"]
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
        if not quote or quote not in source_text_by_block.get(trigger_block, ""):
            errors.append(f"{prefix}.trigger_evidence not_source_substring")
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
