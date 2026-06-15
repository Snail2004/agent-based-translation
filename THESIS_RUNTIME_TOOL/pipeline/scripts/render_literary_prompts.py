from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[2]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.eval.thesis_scoring import normalize_apostrophe
from pipeline.prepass.literary_context import build_literary_builder_context_pack
from pipeline.prepass.prompt import build_messages as build_builder_messages
from pipeline.prepass.registry import PrepassRegistry
from pipeline.retrieval.context_builder import build_context_pack, plan_anchors
from pipeline.translate.prompt import build_messages as build_translator_messages
from pipeline.translate.prompt import prompt_version_for_config
from pipeline.translate.profiles import get_profile
from pipeline.translate.windower import Window, build_windows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render real literary Builder/Translator prompts for offline review."
    )
    parser.add_argument(
        "--db",
        default="data/jobs/treasure_island_p2/memory.sqlite3",
        help="Frozen TI memory DB. Relative paths resolve under THESIS_RUNTIME_TOOL.",
    )
    parser.add_argument(
        "--chapters",
        default="2,3",
        help="Comma or space separated chapters, e.g. 2,3 or ch02 ch03.",
    )
    parser.add_argument(
        "--out",
        default="data/reports/literary_prompt_samples.txt",
        help="Index/summary output. Full prompts are written to --builder-out and --translator-out.",
    )
    parser.add_argument(
        "--builder-out",
        default="data/reports/literary_builder_prompt_sample.txt",
        help="Full Builder prompt output.",
    )
    parser.add_argument(
        "--translator-out",
        default="data/reports/literary_translator_s1_prompt_sample.txt",
        help="Full Translator S1 prompt output.",
    )
    parser.add_argument(
        "--audit-out",
        default="data/reports/literary_builder_context_audit.json",
        help="Machine-readable audit output.",
    )
    parser.add_argument(
        "--registry-out",
        default="data/reports/literary_registry_snapshot.json",
        help="Full frozen Builder registry snapshot output.",
    )
    parser.add_argument(
        "--density-out",
        default="data/reports/literary_builder_density_audit.json",
        help="Builder output density audit report.",
    )
    parser.add_argument(
        "--density-threshold",
        type=float,
        default=2.5,
        help="Warn when glossary density is this many times the prior chapter.",
    )
    parser.add_argument(
        "--preflight-table",
        action="store_true",
        help="Print Builder preflight rows for every requested chapter.",
    )
    parser.add_argument("--profile", default="literary_v1")
    parser.add_argument(
        "--prepass-dir",
        default="data/prepass/treasure_island_pilot",
        help=(
            "Optional prepass artifact directory. When available, the Builder "
            "sample for the last requested chapter uses registry accumulated "
            "from prior requested chapters only, matching pre-run chronology."
        ),
    )
    parser.add_argument("--builder-budget", type=int, default=600)
    parser.add_argument("--translator-context-budget", type=int, default=500)
    args = parser.parse_args()

    db_path = _tool_path(args.db)
    out_path = _tool_path(args.out)
    builder_out_path = _tool_path(args.builder_out)
    translator_out_path = _tool_path(args.translator_out)
    audit_path = _tool_path(args.audit_out)
    registry_out_path = _tool_path(args.registry_out)
    density_out_path = _tool_path(args.density_out)
    prepass_dir = _tool_path(args.prepass_dir)
    chapter_ids = _parse_chapters(args.chapters)
    profile = get_profile(args.profile)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        doc_id = _single_doc_id(conn)
        builder_chapter_id = chapter_ids[-1]
        builder_chapter = _fetch_chapter(conn, doc_id, builder_chapter_id)
        registry, registry_source = _registry_for_builder_sample(
            conn,
            doc_id,
            chapter_ids,
            prepass_dir,
        )
        registry_snapshot = _registry_snapshot_from_db(conn, doc_id)
        builder_pack = build_literary_builder_context_pack(
            builder_chapter,
            registry,
            budget_tokens=args.builder_budget,
        )
        builder_messages = build_builder_messages(
            builder_chapter,
            builder_pack.render_context(),
            mode="literary",
        )
        builder_preflight = _build_builder_preflight(
            conn,
            doc_id,
            chapter_ids,
            prepass_dir,
            budget_tokens=args.builder_budget,
        )
        density_audit = _build_density_audit(
            conn,
            doc_id,
            chapter_ids,
            prepass_dir,
            density_threshold=args.density_threshold,
        )

        windows = build_windows(
            conn,
            doc_id,
            chapter_ids,
            block_types=profile.translatable_block_types,
        )
        translator_window, translator_blocks, translator_pack = _select_translator_sample(
            conn,
            windows,
            profile_name=profile.name,
            context_budget=args.translator_context_budget,
        )
        translator_prompt_version = prompt_version_for_config("S1", profile.name)
        translator_messages = build_translator_messages(
            translator_blocks,
            prompt_version=translator_prompt_version,
            config="S1",
            context_pack=translator_pack,
            profile_name=profile.name,
        )
    finally:
        conn.close()

    builder_prompt_tokens = estimate_prompt_tokens(
        builder_messages,
        response_format={"type": "json_object"},
    )
    translator_prompt_tokens = estimate_prompt_tokens(
        translator_messages,
        response_format={"type": "json_object"},
    )

    audit = {
        "manifest": {
            "purpose": "HYG-02 offline prompt/context review; no API call",
            "db": str(db_path),
            "chapters": chapter_ids,
            "profile": profile.name,
        },
        "builder": {
            "chapter_id": builder_chapter["chapter_id"],
            "registry_source": registry_source,
            "prompt_tokens_est": builder_prompt_tokens,
            "context_pack": builder_pack.to_dict(),
        },
        "translator": {
            "window_id": translator_window.window_id,
            "block_ids": translator_window.block_ids,
            "prompt_version": translator_prompt_version,
            "prompt_tokens_est": translator_prompt_tokens,
            "context_pack": translator_pack.to_dict(),
        },
        "builder_preflight": builder_preflight,
        "cache_friendliness": _cache_friendliness_report(builder_preflight),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    builder_out_path.parent.mkdir(parents=True, exist_ok=True)
    translator_out_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    registry_out_path.parent.mkdir(parents=True, exist_ok=True)
    density_out_path.parent.mkdir(parents=True, exist_ok=True)
    builder_out_path.write_text(
        _render_single_prompt_file(
            title="HYG-02 Literary Builder Prompt Sample",
            db_path=db_path,
            profile_name=profile.name,
            chapters=chapter_ids,
            metadata={
                "chapter_id": builder_chapter["chapter_id"],
                "prompt_tokens_est": builder_prompt_tokens,
                "context_pack_tokens": builder_pack.token_estimate,
                "context_pack_budget": builder_pack.budget_tokens,
                "registry_source": registry_source,
                "audit_path": str(audit_path),
                "registry_snapshot_path": str(registry_out_path),
                "density_audit_path": str(density_out_path),
            },
            messages=builder_messages,
        ),
        encoding="utf-8",
    )
    translator_out_path.write_text(
        _render_single_prompt_file(
            title="HYG-01 Literary Translator S1 Prompt Sample",
            db_path=db_path,
            profile_name=profile.name,
            chapters=chapter_ids,
            metadata={
                "window_id": translator_window.window_id,
                "block_ids": translator_window.block_ids,
                "prompt_version": translator_prompt_version,
                "prompt_tokens_est": translator_prompt_tokens,
                "context_pack_tokens": translator_pack.token_estimate,
                "audit_path": str(audit_path),
                "registry_snapshot_path": str(registry_out_path),
            },
            messages=translator_messages,
        ),
        encoding="utf-8",
    )
    out_path.write_text(
        _render_index_file(
            db_path=db_path,
            profile_name=profile.name,
            chapter_ids=chapter_ids,
            builder_out_path=builder_out_path,
            builder_prompt_tokens=builder_prompt_tokens,
            builder_pack=builder_pack.to_dict(),
            builder_registry_source=registry_source,
            translator_window=translator_window,
            translator_out_path=translator_out_path,
            translator_pack=translator_pack.to_dict(),
            translator_prompt_tokens=translator_prompt_tokens,
            translator_prompt_version=translator_prompt_version,
            audit_path=audit_path,
            registry_out_path=registry_out_path,
            density_out_path=density_out_path,
            builder_preflight=builder_preflight,
            density_audit=density_audit,
        ),
        encoding="utf-8",
    )
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    registry_out_path.write_text(
        json.dumps(registry_snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    density_out_path.write_text(
        json.dumps(density_audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Prompt index written: {out_path}")
    print(f"Builder prompt written: {builder_out_path}")
    print(f"Translator prompt written: {translator_out_path}")
    print(f"Audit written: {audit_path}")
    print(f"Registry snapshot written: {registry_out_path}")
    print(f"Density audit written: {density_out_path}")
    print(f"Builder prompt est tokens: {builder_prompt_tokens}")
    print(f"Translator S1 prompt est tokens: {translator_prompt_tokens}")
    print(f"Translator prompt version: {translator_prompt_version}")
    if args.preflight_table:
        print(_render_preflight_table(builder_preflight))
    return 0


def _tool_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return TOOL_ROOT / path


def _parse_chapters(value: str) -> list[str]:
    raw = [item.strip() for part in value.split() for item in part.split(",")]
    result: list[str] = []
    for item in raw:
        if not item:
            continue
        if item.isdigit():
            result.append(f"ch{int(item):02d}")
        elif re_match := re.search(r"(\d+)$", item):
            if item.lower().startswith("ch"):
                result.append(item)
            else:
                result.append(f"ch{int(re_match.group(1)):02d}")
        else:
            result.append(item)
    return result


def _single_doc_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT doc_id FROM documents ORDER BY doc_id LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("No document found in DB")
    return str(row["doc_id"])


def _fetch_chapter(conn: sqlite3.Connection, doc_id: str, chapter_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE doc_id = ?
          AND (chapter_id = ? OR chapter_id LIKE ?)
        ORDER BY order_index
        """,
        (doc_id, chapter_id, f"%{chapter_id}"),
    ).fetchall()
    if not rows:
        raise SystemExit(f"Chapter not found in DB: {chapter_id}")
    return {
        "chapter_id": str(rows[0]["chapter_id"]),
        "blocks": [dict(row) for row in rows],
    }


def _registry_from_db(conn: sqlite3.Connection, doc_id: str) -> PrepassRegistry:
    registry = PrepassRegistry()
    for row in conn.execute(
        """
        SELECT glossary_id, source_term, target_term, term_type, do_not_translate,
               occurrences_count, last_block_id
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY source_term
        """,
        (doc_id,),
    ):
        key = str(row["source_term"] or "").casefold()
        registry.glossary[key] = {
            "source_term": str(row["source_term"] or ""),
            "proposed_target_vi": str(row["target_term"] or ""),
            "do_not_translate": bool(row["do_not_translate"]),
            "category": str(row["term_type"] or "other"),
            "block_ids": [str(row["last_block_id"])] if row["last_block_id"] else [],
            "occurrences_count": int(row["occurrences_count"] or 0),
        }

    for row in conn.execute(
        """
        SELECT entity_id, canonical_source, canonical_target, entity_type,
               aliases_source_json, aliases_target_json, first_block_id, latest_block_id
        FROM entities
        WHERE doc_id = ?
        ORDER BY entity_id
        """,
        (doc_id,),
    ):
        entity_id = str(row["entity_id"])
        registry.entities[entity_id] = {
            "entity_id": entity_id,
            "canonical_source": str(row["canonical_source"] or ""),
            "proposed_target_vi": str(row["canonical_target"] or ""),
            "entity_type": str(row["entity_type"] or "other"),
            "aliases_source": _json_list(row["aliases_source_json"]),
            "aliases_target_vi": _json_list(row["aliases_target_json"]),
        }
        for block_id in [row["first_block_id"], row["latest_block_id"]]:
            chapter = _chapter_for_block(conn, str(block_id or ""))
            if chapter:
                registry.entity_chapters.setdefault(entity_id, set()).add(chapter)

    for row in conn.execute(
        """
        SELECT relation_id, source_entity_id, target_entity_id, relation_type,
               state_label, address_policy_json, notes
        FROM entity_relations
        WHERE doc_id = ?
        ORDER BY relation_id
        """,
        (doc_id,),
    ):
        policy = _json_dict(row["address_policy_json"])
        relation_id = str(row["relation_id"])
        left = str(row["source_entity_id"])
        right = str(row["target_entity_id"])
        registry.relations[(left, right)] = {
            "relation_id": relation_id,
            "a": left,
            "b": right,
            "relation": str(row["relation_type"] or ""),
            "state_label": str(row["state_label"] or ""),
            "address_a_to_b_vi": str(policy.get("a_to_b") or ""),
            "address_b_to_a_vi": str(policy.get("b_to_a") or ""),
            "notes": str(row["notes"] or ""),
        }
    return registry


def _registry_for_builder_sample(
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
    prepass_dir: Path,
) -> tuple[PrepassRegistry, str]:
    prior_chapters = chapter_ids[:-1]
    registry = PrepassRegistry()
    loaded: list[str] = []
    if not prior_chapters:
        return registry, "empty_registry_first_chapter"
    if prepass_dir.exists() and prior_chapters:
        for chapter_id in prior_chapters:
            artifact_path = _prepass_artifact_for_chapter(prepass_dir, conn, doc_id, chapter_id)
            if artifact_path is None:
                raise SystemExit(
                    "Missing prior prepass artifact for chronology-safe render: "
                    f"{chapter_id} in {prepass_dir}"
                )
            registry.merge(json.loads(artifact_path.read_text(encoding="utf-8")))
            loaded.append(str(artifact_path))
    if loaded:
        return registry, f"prepass_artifacts_prior_chapters:{loaded}"
    raise SystemExit(
        f"No prior prepass artifacts loaded from {prepass_dir}; refusing frozen DB fallback."
    )


def _prepass_artifact_for_chapter(
    prepass_dir: Path,
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_id: str,
) -> Path | None:
    chapter = _resolve_chapter_id(conn, doc_id, chapter_id)
    candidates = [
        prepass_dir / f"{chapter}.json",
        prepass_dir / f"{chapter_id}.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    matches = sorted(prepass_dir.glob(f"*{chapter_id}.json"))
    return matches[0] if matches else None


def _resolve_chapter_id(conn: sqlite3.Connection, doc_id: str, chapter_id: str) -> str:
    row = conn.execute(
        """
        SELECT DISTINCT chapter_id
        FROM blocks
        WHERE doc_id = ? AND (chapter_id = ? OR chapter_id LIKE ?)
        ORDER BY chapter_id
        LIMIT 1
        """,
        (doc_id, chapter_id, f"%{chapter_id}"),
    ).fetchone()
    return str(row["chapter_id"]) if row is not None else chapter_id


def _registry_snapshot_from_db(conn: sqlite3.Connection, doc_id: str) -> dict[str, Any]:
    glossary = _rows(
        conn,
        """
        SELECT glossary_id, source_term, target_term, term_type, scope, chapter_id,
               do_not_translate, allowed_variants_json, forbidden_variants_json,
               occurrences_count, last_block_id, status
        FROM glossary_entries
        WHERE doc_id = ?
        ORDER BY source_term
        """,
        (doc_id,),
    )
    entities = _rows(
        conn,
        """
        SELECT entity_id, canonical_source, canonical_target, entity_type, role,
               first_block_id, latest_block_id, aliases_source_json,
               aliases_target_json, preferred_vietnamese_forms_json, status
        FROM entities
        WHERE doc_id = ?
        ORDER BY entity_id
        """,
        (doc_id,),
    )
    relations = _rows(
        conn,
        """
        SELECT relation_id, source_entity_id, target_entity_id, relation_type,
               state_label, valid_from_block_id, valid_to_block_id,
               address_policy_json, notes
        FROM entity_relations
        WHERE doc_id = ?
        ORDER BY relation_id
        """,
        (doc_id,),
    )
    return {
        "manifest": {
            "purpose": "Full frozen literary Builder registry for prompt review; not all of this is injected.",
            "doc_id": doc_id,
            "counts": {
                "glossary": len(glossary),
                "entities": len(entities),
                "relations": len(relations),
            },
        },
        "glossary": glossary,
        "entities": entities,
        "relations": relations,
    }


def _build_builder_preflight(
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
    prepass_dir: Path,
    *,
    budget_tokens: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, chapter_id in enumerate(chapter_ids):
        chapter = _fetch_chapter(conn, doc_id, chapter_id)
        registry, registry_source = _registry_for_builder_sample(
            conn,
            doc_id,
            chapter_ids[: index + 1],
            prepass_dir,
        )
        pack = build_literary_builder_context_pack(
            chapter,
            registry,
            budget_tokens=budget_tokens,
        )
        messages = build_builder_messages(chapter, pack.render_context(), mode="literary")
        prompt_tokens = estimate_prompt_tokens(
            messages,
            response_format={"type": "json_object"},
        )
        source_tokens = _rough_tokens(_chapter_source_text(chapter))
        system_hash = _sha256(messages[0]["content"])
        rows.append(
            {
                "chapter_id": str(chapter["chapter_id"]),
                "registry_source": registry_source,
                "source_tokens_est": source_tokens,
                "context_pack_tokens": pack.token_estimate,
                "prompt_tokens_est": prompt_tokens,
                "included": len(pack.included),
                "excluded": len(pack.excluded),
                "dropped": len(pack.dropped_by_budget),
                "status": _builder_prompt_status(prompt_tokens),
                "system_prefix_sha256": system_hash,
                "context_pack": pack.to_dict(),
            }
        )
    return rows


def _build_density_audit(
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
    prepass_dir: Path,
    *,
    density_threshold: float,
) -> dict[str, Any]:
    seen_terms: set[str] = set()
    previous_density: float | None = None
    chapters: list[dict[str, Any]] = []
    for chapter_id in chapter_ids:
        chapter = _fetch_chapter(conn, doc_id, chapter_id)
        resolved_chapter_id = str(chapter["chapter_id"])
        source_text = _chapter_source_text(chapter)
        source_tokens = _rough_tokens(source_text)
        artifact_path = _prepass_artifact_for_chapter(prepass_dir, conn, doc_id, chapter_id)
        if artifact_path is None:
            chapters.append(
                {
                    "chapter_id": resolved_chapter_id,
                    "status": "MISSING_ARTIFACT",
                    "artifact_path": None,
                    "source_tokens_est": source_tokens,
                }
            )
            continue
        output = json.loads(artifact_path.read_text(encoding="utf-8"))
        terms = [term for term in output.get("glossary_candidates") or [] if isinstance(term, dict)]
        category_distribution: dict[str, int] = {}
        occurrence_counts: dict[str, int] = {}
        sample_new_terms: list[dict[str, Any]] = []
        for term in terms:
            source = str(term.get("source_term") or "").strip()
            if not source:
                continue
            category = str(term.get("category") or "other")
            category_distribution[category] = category_distribution.get(category, 0) + 1
            occurrence_counts[source] = _count_source_matches(source_text, source)
            key = source.casefold()
            if key not in seen_terms and len(sample_new_terms) < 20:
                sample_new_terms.append(
                    {
                        "source_term": source,
                        "proposed_target_vi": str(term.get("proposed_target_vi") or ""),
                        "category": category,
                        "occurrences_in_chapter": occurrence_counts[source],
                    }
                )
            seen_terms.add(key)
        glossary_count = len(terms)
        density = (glossary_count / source_tokens * 1000.0) if source_tokens else 0.0
        density_anomaly = (
            previous_density is not None
            and previous_density > 0
            and density >= previous_density * density_threshold
        )
        chapters.append(
            {
                "chapter_id": resolved_chapter_id,
                "status": "REVIEW_REQUIRED" if density_anomaly else "OK",
                "artifact_path": str(artifact_path),
                "source_tokens_est": source_tokens,
                "glossary_count": glossary_count,
                "glossary_per_1k_source_tokens": round(density, 4),
                "hapax_count": sum(1 for count in occurrence_counts.values() if count <= 1),
                "category_distribution": dict(sorted(category_distribution.items())),
                "sample_new_terms": sample_new_terms,
                "density_anomaly": density_anomaly,
                "previous_density_per_1k": (
                    round(previous_density, 4) if previous_density is not None else None
                ),
                "density_threshold_multiplier": density_threshold,
            }
        )
        previous_density = density
    return {
        "manifest": {
            "purpose": "HYG-02 Builder output density audit; offline, no API call",
            "doc_id": doc_id,
            "chapters": chapter_ids,
            "prepass_dir": str(prepass_dir),
            "density_threshold_multiplier": density_threshold,
        },
        "chapters": chapters,
        "status_counts": _status_counts(chapters),
    }


def _cache_friendliness_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    hashes = [str(row["system_prefix_sha256"]) for row in rows]
    return {
        "system_prefix_byte_identical": len(set(hashes)) <= 1,
        "system_prefix_sha256": hashes[0] if hashes else None,
        "chapter_count": len(rows),
        "context_pack_deterministic_sort": True,
        "no_timestamp_or_random_in_prompt": True,
    }


def _render_preflight_table(rows: list[dict[str, Any]]) -> str:
    header = (
        "chapter_id | source_tokens | context_pack_tokens | prompt_tokens | "
        "included/excluded/dropped | status"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            " | ".join(
                [
                    str(row["chapter_id"]),
                    str(row["source_tokens_est"]),
                    str(row["context_pack_tokens"]),
                    str(row["prompt_tokens_est"]),
                    f"{row['included']}/{row['excluded']}/{row['dropped']}",
                    str(row["status"]),
                ]
            )
        )
    return "\n".join(lines)


def _builder_prompt_status(prompt_tokens: int) -> str:
    if prompt_tokens <= 8000:
        return "OK"
    if prompt_tokens <= 12000:
        return "WARN"
    if prompt_tokens <= 20000:
        return "SPLIT_REQUIRED"
    return "ABORT"


def _chapter_source_text(chapter: dict[str, Any]) -> str:
    parts = [
        str(block.get("clean_text") or block.get("source_text") or "")
        for block in chapter.get("blocks") or []
    ]
    return "\n".join(parts)


def _rough_tokens(text: str) -> int:
    return max(1, len(str(text)) // 4)


def _count_source_matches(text: str, source_term: str) -> int:
    normalized_text = normalize_apostrophe(str(text)).casefold()
    normalized_source = normalize_apostrophe(str(source_term).strip()).casefold()
    if not normalized_source:
        return 0
    pattern = rf"(?<!\w){re.escape(normalized_source)}(?!\w)"
    return len(re.findall(pattern, normalized_text, flags=re.UNICODE))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "UNKNOWN")
        result[status] = result.get(status, 0) + 1
    return dict(sorted(result.items()))


def _chapter_for_block(conn: sqlite3.Connection, block_id: str) -> str | None:
    if not block_id:
        return None
    row = conn.execute(
        "SELECT chapter_id FROM blocks WHERE block_id = ?",
        (block_id,),
    ).fetchone()
    return str(row["chapter_id"]) if row is not None else None


def _select_translator_sample(
    conn: sqlite3.Connection,
    windows: list[Window],
    *,
    profile_name: str,
    context_budget: int,
) -> tuple[Window, list[dict[str, Any]], Any]:
    if not windows:
        raise SystemExit("No translation windows found")
    best: tuple[Window, list[dict[str, Any]], Any] | None = None
    best_score = -1
    for window in windows:
        blocks = _fetch_window_blocks(conn, window)
        anchors = plan_anchors(conn, blocks, profile_name=profile_name)
        pack = build_context_pack(conn, window, anchors, budget_tokens=context_budget)
        score = len(pack.glossary_lines) + len(pack.entity_lines) + len(pack.address_lines)
        if best is None or score > best_score:
            best = (window, blocks, pack)
            best_score = score
    assert best is not None
    return best


def _fetch_window_blocks(conn: sqlite3.Connection, window: Window) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(window.block_ids))
    rows = conn.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE block_id IN ({placeholders})
        ORDER BY order_index
        """,
        list(window.block_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _render_index_file(
    *,
    db_path: Path,
    profile_name: str,
    chapter_ids: list[str],
    builder_out_path: Path,
    builder_prompt_tokens: int,
    builder_pack: dict[str, Any],
    builder_registry_source: str,
    translator_window: Window,
    translator_out_path: Path,
    translator_pack: dict[str, Any],
    translator_prompt_tokens: int,
    translator_prompt_version: str,
    audit_path: Path,
    registry_out_path: Path,
    density_out_path: Path,
    builder_preflight: list[dict[str, Any]],
    density_audit: dict[str, Any],
) -> str:
    sections = [
        "# HYG-02 Literary Prompt Review Index",
        "",
        "No API call was made. Full prompts are split into separate files.",
        f"DB: {db_path}",
        f"Profile: {profile_name}",
        f"Chapters: {chapter_ids}",
        "",
        "## Files",
        f"- Builder prompt: {builder_out_path}",
        f"- Translator S1 prompt: {translator_out_path}",
        f"- Context audit: {audit_path}",
        f"- Full frozen registry snapshot: {registry_out_path}",
        f"- Builder density audit: {density_out_path}",
        "",
        "## Important distinction",
        "- `literary_registry_snapshot.json` is the full registry already built and frozen in DB.",
        "- `literary_builder_context_audit.json` shows the filtered context pack selected from that registry for the Builder prompt.",
        "- The Builder prompt does NOT receive the full registry snapshot.",
        "- Builder preview uses prior-chapter prepass artifacts only; it refuses frozen-DB fallback for chronology safety.",
        "",
        "## Builder Summary",
        json.dumps(
            {
                "prompt_tokens_est": builder_prompt_tokens,
                "registry_source": builder_registry_source,
                "context_pack_counts": builder_pack.get("counts"),
                "context_pack_tokens": builder_pack.get("token_estimate"),
                "context_pack_budget": builder_pack.get("budget_tokens"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "",
        "## Builder Full-Set Preflight",
        _render_preflight_table(builder_preflight),
        "",
        "## Builder Density Summary",
        json.dumps(
            {
                "status_counts": density_audit.get("status_counts"),
                "density_threshold_multiplier": density_audit.get("manifest", {}).get(
                    "density_threshold_multiplier"
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "",
        "## Translator Summary",
        json.dumps(
            {
                "window_id": translator_window.window_id,
                "block_ids": translator_window.block_ids,
                "prompt_version": translator_prompt_version,
                "prompt_tokens_est": translator_prompt_tokens,
                "context_pack_tokens": translator_pack.get("token_estimate"),
                "anchors_count": translator_pack.get("anchors_count"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "",
    ]
    return "\n".join(sections)


def _render_single_prompt_file(
    *,
    title: str,
    db_path: Path,
    profile_name: str,
    chapters: list[str],
    metadata: dict[str, Any],
    messages: list[dict[str, str]],
) -> str:
    sections = [
        f"# {title}",
        "",
        "No API call was made. This is a full prompt rendered from current code; source blocks come from DB and Builder registry context follows the chronology source in metadata.",
        f"DB: {db_path}",
        f"Profile: {profile_name}",
        f"Chapters: {chapters}",
        "",
        "## Metadata",
        json.dumps(metadata, ensure_ascii=False, indent=2),
        "",
        "## Prompt",
        *_render_messages(messages),
        "",
    ]
    return "\n".join(sections)


def _render_messages(messages: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        lines.append(f"### Message {index}: {message.get('role', '')}")
        lines.append(message.get("content", ""))
        lines.append("")
    return lines


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


if __name__ == "__main__":
    raise SystemExit(main())
