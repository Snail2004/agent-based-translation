from __future__ import annotations

import argparse
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
        help="Prompt sample text output.",
    )
    parser.add_argument(
        "--audit-out",
        default="data/reports/literary_builder_context_audit.json",
        help="Machine-readable audit output.",
    )
    parser.add_argument("--profile", default="literary_v1")
    parser.add_argument("--builder-budget", type=int, default=600)
    parser.add_argument("--translator-context-budget", type=int, default=500)
    args = parser.parse_args()

    db_path = _tool_path(args.db)
    out_path = _tool_path(args.out)
    audit_path = _tool_path(args.audit_out)
    chapter_ids = _parse_chapters(args.chapters)
    profile = get_profile(args.profile)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        doc_id = _single_doc_id(conn)
        builder_chapter_id = chapter_ids[-1]
        builder_chapter = _fetch_chapter(conn, doc_id, builder_chapter_id)
        registry = _registry_from_db(conn, doc_id)
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
            "purpose": "HYG-01 offline prompt/context review; no API call",
            "db": str(db_path),
            "chapters": chapter_ids,
            "profile": profile.name,
        },
        "builder": {
            "chapter_id": builder_chapter["chapter_id"],
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
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _render_prompt_file(
            db_path=db_path,
            profile_name=profile.name,
            chapter_ids=chapter_ids,
            builder_messages=builder_messages,
            builder_pack=builder_pack.to_dict(),
            builder_prompt_tokens=builder_prompt_tokens,
            translator_window=translator_window,
            translator_messages=translator_messages,
            translator_pack=translator_pack.to_dict(),
            translator_prompt_tokens=translator_prompt_tokens,
            translator_prompt_version=translator_prompt_version,
        ),
        encoding="utf-8",
    )
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Prompt samples written: {out_path}")
    print(f"Audit written: {audit_path}")
    print(f"Builder prompt est tokens: {builder_prompt_tokens}")
    print(f"Translator S1 prompt est tokens: {translator_prompt_tokens}")
    print(f"Translator prompt version: {translator_prompt_version}")
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


def _render_prompt_file(
    *,
    db_path: Path,
    profile_name: str,
    chapter_ids: list[str],
    builder_messages: list[dict[str, str]],
    builder_pack: dict[str, Any],
    builder_prompt_tokens: int,
    translator_window: Window,
    translator_messages: list[dict[str, str]],
    translator_pack: dict[str, Any],
    translator_prompt_tokens: int,
    translator_prompt_version: str,
) -> str:
    sections = [
        "# HYG-01 Literary Prompt Samples",
        "",
        "No API call was made. These prompts are rendered from the frozen DB and current code.",
        f"DB: {db_path}",
        f"Profile: {profile_name}",
        f"Chapters: {chapter_ids}",
        "",
        "## Builder Context Audit Summary",
        json.dumps(
            {
                "prompt_tokens_est": builder_prompt_tokens,
                "context_pack": builder_pack,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "",
        "## Builder Prompt",
        *_render_messages(builder_messages),
        "",
        "## Translator Context Audit Summary",
        json.dumps(
            {
                "window_id": translator_window.window_id,
                "block_ids": translator_window.block_ids,
                "prompt_version": translator_prompt_version,
                "prompt_tokens_est": translator_prompt_tokens,
                "context_pack": translator_pack,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "",
        "## Translator S1 Prompt",
        *_render_messages(translator_messages),
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


if __name__ == "__main__":
    raise SystemExit(main())
