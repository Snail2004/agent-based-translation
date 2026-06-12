from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Window:
    """One translation window = a sequence of 1–N blocks sharing a single LLM call."""

    window_id: str      # "w_<chapter>_<nnn>", e.g. "w_ch02_001"
    block_ids: list[str]
    est_src_tokens: int   # estimated source tokens (chars / 4)

    def to_dict(self) -> dict:
        return asdict(self)


def build_windows(
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
    *,
    target_tokens: int = 1100,
    max_blocks: int = 8,
) -> list[Window]:
    """Partition blocks of given chapters into WINDOWs following LOCK §5 rules.

    Rules (all code-deterministic):
    1. Windows never cross chapter boundaries.
    2. A block that alone exceeds target_tokens gets its own 1-block window (oversize).
    3. Budget = sum of block est_tokens; block est = len(text) // 4 (chars → tokens).
    4. Consecutive dialogue blocks (block_type='dialogue') that together fit budget
       are kept in the same window — no mid-dialogue cut.
    5. A new window STARTS at every block that is a `valid_from_block_id` of any
       entity_relations row in the frozen DB (phase-change boundary).
    6. Maximum block count per window = max_blocks.
    7. Fully deterministic: sorted blocks, window_id counter increments per chapter.
    """

    blocks = _fetch_blocks(conn, doc_id, chapter_ids)
    phase_boundary_ids = _fetch_phase_boundaries(conn, doc_id)

    chapters_sorted = sorted(
        {str(b["chapter_id"]) for b in blocks},
        key=lambda c: (
            [int(m.group(1)) for m in re.finditer(r"(\d+)", c)],
            c,
        ),
    )

    windows: list[Window] = []
    for chapter_id in chapters_sorted:
        chapter_blocks = [b for b in blocks if str(b["chapter_id"]) == chapter_id]
        chapter_blocks.sort(key=lambda b: int(b["order_index"]))

        window_counter = 0
        current_ids: list[str] = []
        current_tokens = 0
        in_dialogue_run = False

        for block in chapter_blocks:
            block_id = str(block["block_id"])
            text = str(block.get("clean_text") or block.get("source_text") or "")
            est_tokens = _est_tokens(text)
            is_dialogue = str(block.get("block_type") or "") == "dialogue"
            is_phase_boundary = block_id in phase_boundary_ids

            if is_phase_boundary and current_ids:
                # Close current window before starting new at boundary
                window_counter += 1
                windows.append(_make_window(chapter_id, window_counter, current_ids, current_tokens))
                current_ids = []
                current_tokens = 0
                in_dialogue_run = False

            # Check if this block alone oversizes — window of 1 oversize block
            if est_tokens > target_tokens:
                if current_ids:
                    window_counter += 1
                    windows.append(_make_window(chapter_id, window_counter, current_ids, current_tokens))
                    current_ids = []
                    current_tokens = 0
                    in_dialogue_run = False
                # The oversize block gets its own window
                window_counter += 1
                windows.append(_make_window(chapter_id, window_counter, [block_id], est_tokens))
                continue

            # Would adding this block exceed token budget or block count?
            over_token = (current_tokens + est_tokens) > target_tokens
            over_block = len(current_ids) >= max_blocks
            at_dialogue_boundary = (not is_dialogue) and in_dialogue_run

            should_flush = over_token or over_block or at_dialogue_boundary

            if should_flush and current_ids:
                window_counter += 1
                windows.append(_make_window(chapter_id, window_counter, current_ids, current_tokens))
                current_ids = []
                current_tokens = 0
                in_dialogue_run = False

            current_ids.append(block_id)
            current_tokens += est_tokens
            in_dialogue_run = is_dialogue

        if current_ids:
            window_counter += 1
            windows.append(_make_window(chapter_id, window_counter, current_ids, current_tokens))

    return windows


def _make_window(
    chapter_id: str,
    counter: int,
    block_ids: list[str],
    est_tokens: int,
) -> Window:
    # Normalize chapter_id: "ti_ch02" → "ch02", "ch02" → "ch02"
    chapter_slug = _normalize_chapter_slug(chapter_id)
    window_id = f"w_{chapter_slug}_{counter:03d}"
    return Window(window_id=window_id, block_ids=block_ids, est_src_tokens=est_tokens)


def _normalize_chapter_slug(chapter_id: str) -> str:
    """Strip any prefix so 'ti_ch02' or 'treasure_island_ch02' → 'ch02'."""
    m = re.search(r"(ch\d+)", chapter_id, re.IGNORECASE)
    return m.group(1) if m else chapter_id


def _est_tokens(text: str) -> int:
    """Rough token estimate = chars / 4 (matches quota estimator in llm_client)."""
    return max(1, len(text) // 4)


def _fetch_blocks(
    conn: sqlite3.Connection,
    doc_id: str,
    chapter_ids: list[str],
) -> list[dict]:
    """Fetch all blocks for given doc + chapters, sorted by order_index.

    chapter_ids may be full IDs (e.g. 'treasure_island_ch02') or suffixes
    (e.g. 'ch02').  We resolve each to the matching full chapter_id(s) by
    querying the blocks table, then select those.
    """
    if not chapter_ids:
        return []

    # Resolve suffix → full chapter_id via the DB
    all_rows = conn.execute(
        "SELECT DISTINCT chapter_id FROM blocks WHERE doc_id = ?",
        (doc_id,),
    ).fetchall()
    db_chapters = {str(r["chapter_id"]) for r in all_rows}

    # Match each input chapter_id (full or suffix) against db_chapters
    matched_chapters: list[str] = []
    for requested in chapter_ids:
        found = False
        for db_ch in db_chapters:
            # Match if db_chapter ends with the requested suffix, or is exact
            if db_ch == requested or db_ch.endswith("_" + requested) or db_ch.endswith(requested):
                matched_chapters.append(db_ch)
                found = True
        if not found:
            # Try prefix match: if user types 'ch02', match 'treasure_island_ch02'
            for db_ch in db_chapters:
                # Check if db_ch ends with requested pattern (e.g. '_ch02' or just 'ch02')
                if db_ch.endswith("_" + requested):
                    matched_chapters.append(db_ch)
                    found = True
                    break
            if not found:
                # Last resort: any db_chapter containing the suffix
                for db_ch in db_chapters:
                    import re as _re
                    if _re.search(rf"(^|_){_re.escape(requested)}$", db_ch, re.IGNORECASE):
                        matched_chapters.append(db_ch)
                        found = True
                        break

    if not matched_chapters:
        return []

    placeholders = ",".join("?" * len(matched_chapters))
    rows = conn.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE doc_id = ? AND chapter_id IN ({placeholders})
        ORDER BY order_index
        """,
        [doc_id] + matched_chapters,
    ).fetchall()
    return [dict(row) for row in rows]


def _fetch_phase_boundaries(conn: sqlite3.Connection, doc_id: str) -> set[str]:
    """All block_ids that are valid_from_block_id of some entity_relations row.

    Gracefully handles missing table (test fixtures, fresh DB).
    """
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT valid_from_block_id
            FROM entity_relations
            WHERE doc_id = ? AND valid_from_block_id IS NOT NULL
            """,
            (doc_id,),
        ).fetchall()
        return {str(row["valid_from_block_id"]) for row in rows}
    except sqlite3.OperationalError:
        return set()
