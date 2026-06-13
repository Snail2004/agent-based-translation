from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GlossaryGoldEntry:
    gold_id: str
    source_term: str
    target_term: str
    discussion_url: str
    letter: str
    source_line: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_glossary(glossary_path: str | Path) -> list[GlossaryGoldEntry]:
    """Parse D2L glossary.md table rows into EN->VI eval-only gold entries."""

    path = Path(glossary_path)
    entries: list[GlossaryGoldEntry] = []
    seen_pairs: set[tuple[str, str]] = set()
    current_letter = ""
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        letter_match = re.match(r"^##\s+([A-Z])\s*$", line)
        if letter_match:
            current_letter = letter_match.group(1)
            continue
        if not line.startswith("|") or "|" not in line[1:]:
            continue
        cells = [_clean_cell(cell) for cell in line.strip("|").split("|")]
        if len(cells) < 2 or _is_header_or_separator(cells):
            continue
        source_term = cells[0].strip()
        target_term = cells[1].strip()
        if not source_term or not target_term:
            continue
        pair_key = (source_term.casefold(), target_term.casefold())
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        discussion_url = cells[2].strip() if len(cells) > 2 else ""
        entries.append(
            GlossaryGoldEntry(
                gold_id=_gold_id(source_term, target_term),
                source_term=source_term,
                target_term=target_term,
                discussion_url=discussion_url,
                letter=current_letter,
                source_line=line_no,
            )
        )
    return entries


def store_glossary_gold(
    conn: sqlite3.Connection,
    doc_id: str,
    entries: list[GlossaryGoldEntry],
    *,
    source_path: str | Path,
    source_commit: str,
) -> int:
    """Store D2L glossary gold in eval_glossary_gold, never in glossary_entries."""

    conn.execute("DELETE FROM eval_glossary_gold WHERE doc_id = ?", (doc_id,))
    relative_source = str(Path(source_path).as_posix())
    conn.executemany(
        """
        INSERT INTO eval_glossary_gold (
          gold_id, doc_id, source_term, target_term, discussion_url,
          source_path, source_commit, source_line, subset_tag
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'd2l_glossary')
        """,
        [
            (
                entry.gold_id,
                doc_id,
                entry.source_term,
                entry.target_term,
                entry.discussion_url,
                relative_source,
                source_commit,
                entry.source_line,
            )
            for entry in entries
        ],
    )
    return len(entries)


def _clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _is_header_or_separator(cells: list[str]) -> bool:
    first = cells[0].strip().casefold()
    second = cells[1].strip().casefold() if len(cells) > 1 else ""
    if first == "english" or "tiếng việt" in second:
        return True
    compact = "".join(cells).replace(":", "").replace("-", "").strip()
    return compact == ""


def _gold_id(source_term: str, target_term: str) -> str:
    digest = hashlib.sha256(f"{source_term}\0{target_term}".encode("utf-8")).hexdigest()
    return f"d2l_gold_{digest[:16]}"
