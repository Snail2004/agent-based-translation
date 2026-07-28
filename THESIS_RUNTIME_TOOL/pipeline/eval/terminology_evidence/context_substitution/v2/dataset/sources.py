from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.contract import (
    FreezeValidationError,
    sha256_bytes,
    sha256_text,
    stable_id,
)


TARGET_KEYS = (
    "target_proposals",
    "alternative_targets",
    "rejected_target_proposals",
    "pending_target_proposals",
)
CANDIDATE_ID_KEYS = (
    "candidate_id",
    "member_candidate_id",
    "source_candidate_id",
)
CANDIDATE_ID_LIST_KEYS = (
    "source_member_candidate_ids",
    "member_candidate_ids",
    "candidate_ids",
)
SOURCE_KEYS = (
    "canonical_source",
    "source_surface",
    "normalized_surface",
    "source_term",
)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FreezeValidationError(f"{path}: file not found") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FreezeValidationError(f"{path}: invalid JSON: {exc}") from exc


def artifact_binding(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {
        "ref": resolved.as_posix(),
        "physical_sha256": sha256_bytes(resolved.read_bytes()),
    }


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold().strip()


def normalize_target(value: str) -> str:
    return " ".join(normalize_text(value).split())


def stable_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_target(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value.strip())
    return result


def glossary_records(glossary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = glossary.get("records")
    if not isinstance(rows, list):
        raise FreezeValidationError("glossary.records: expected list")
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("value"), dict):
            raise FreezeValidationError(f"glossary.records[{index}]: invalid record")
        if row.get("lifecycle") not in {None, "committed"}:
            continue
        result.append(row)
    return result


def candidate_index_rows(
    candidate_index: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    rows = candidate_index.get("candidates")
    if not isinstance(rows, list):
        raise FreezeValidationError("candidate_index.candidates: expected list")
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FreezeValidationError(
                f"candidate_index.candidates[{index}]: expected object"
            )
        candidate_id = row.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise FreezeValidationError(
                f"candidate_index.candidates[{index}].candidate_id: invalid"
            )
        if candidate_id in result:
            raise FreezeValidationError(
                f"candidate_index.candidates[{index}].candidate_id: duplicate"
            )
        result[candidate_id] = row
    return result


def _target_rows(value: Mapping[str, Any]) -> list[tuple[str, str | None, str]]:
    result: list[tuple[str, str | None, str]] = []
    canonical = value.get("canonical_target_vi")
    if isinstance(canonical, str) and canonical.strip():
        result.append(
            (
                canonical.strip(),
                value.get("canonical_applicability")
                if isinstance(value.get("canonical_applicability"), str)
                else None,
                "canonical_target_vi",
            )
        )
    direct = value.get("target_vi")
    if isinstance(direct, str) and direct.strip():
        result.append(
            (
                direct.strip(),
                value.get("applicability")
                if isinstance(value.get("applicability"), str)
                else None,
                "target_vi",
            )
        )
    for key in TARGET_KEYS:
        rows = value.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            target = row.get("target_vi")
            if not isinstance(target, str) or not target.strip():
                continue
            result.append(
                (
                    target.strip(),
                    row.get("applicability")
                    if isinstance(row.get("applicability"), str)
                    else None,
                    key,
                )
            )
    return result


def collect_candidate_evidence(
    *,
    glossary: Mapping[str, Any],
    glossary_path: Path,
    candidate_artifact_paths: Sequence[Path],
) -> dict[str, list[dict[str, Any]]]:
    by_candidate_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    artifact_bindings = {
        path.resolve(): artifact_binding(path) for path in candidate_artifact_paths
    }

    def visit(value: Any, *, path: str, artifact: Path) -> None:
        if isinstance(value, dict):
            targets = _target_rows(value)
            if targets:
                candidate_ids: list[str] = []
                for key in CANDIDATE_ID_KEYS:
                    if isinstance(value.get(key), str):
                        candidate_ids.append(value[key])
                for key in CANDIDATE_ID_LIST_KEYS:
                    if isinstance(value.get(key), list):
                        candidate_ids.extend(
                            row for row in value[key] if isinstance(row, str)
                        )
                source = next(
                    (
                        value[key]
                        for key in SOURCE_KEYS
                        if isinstance(value.get(key), str)
                    ),
                    None,
                )
                binding = artifact_bindings[artifact.resolve()]
                for target, applicability, field in targets:
                    evidence = {
                        "target_vi": target,
                        "applicability": applicability,
                        "source_kind": "pipeline_artifact",
                        "source_artifact_ref": binding["ref"],
                        "source_artifact_sha256": binding["physical_sha256"],
                        "source_json_path": f"{path}.{field}",
                    }
                    for candidate_id in candidate_ids:
                        by_candidate_id[candidate_id].append(evidence)
                    if source:
                        by_source[normalize_text(source)].append(evidence)
            for key, child in value.items():
                visit(child, path=f"{path}.{key}", artifact=artifact)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path=f"{path}[{index}]", artifact=artifact)

    for candidate_path in candidate_artifact_paths:
        visit(load_json(candidate_path), path="$", artifact=candidate_path)

    result: dict[str, list[dict[str, Any]]] = {}
    glossary_binding = artifact_binding(glossary_path)
    for index, record in enumerate(glossary_records(glossary)):
        value = record["value"]
        term_id = str(record.get("record_id") or value.get("entry_id") or index)
        evidence: list[dict[str, Any]] = []
        for target, applicability, field in _target_rows(value):
            evidence.append(
                {
                    "target_vi": target,
                    "applicability": applicability,
                    "source_kind": "sealed_glossary",
                    "source_artifact_ref": glossary_binding["ref"],
                    "source_artifact_sha256": glossary_binding["physical_sha256"],
                    "source_json_path": f"$.records[{index}].value.{field}",
                }
            )
        for candidate_id in value.get("source_member_candidate_ids", []):
            if isinstance(candidate_id, str):
                evidence.extend(by_candidate_id.get(candidate_id, []))
        source = value.get("canonical_source")
        if isinstance(source, str):
            evidence.extend(by_source.get(normalize_text(source), []))

        grouped: dict[str, dict[str, Any]] = {}
        for row in evidence:
            normalized = normalize_target(row["target_vi"])
            if not normalized:
                continue
            current = grouped.setdefault(
                normalized,
                {
                    "target_vi": row["target_vi"],
                    "applicability": row["applicability"],
                    "evidence": [],
                },
            )
            current["evidence"].append(
                {
                    key: row[key]
                    for key in (
                        "source_kind",
                        "source_artifact_ref",
                        "source_artifact_sha256",
                        "source_json_path",
                    )
                }
            )
            if current["applicability"] is None and row["applicability"] is not None:
                current["applicability"] = row["applicability"]
        result[term_id] = sorted(
            grouped.values(),
            key=lambda row: (
                0
                if any(
                    evidence["source_kind"] == "sealed_glossary"
                    and evidence["source_json_path"].endswith(
                        ".canonical_target_vi"
                    )
                    for evidence in row["evidence"]
                )
                else 1,
                normalize_target(row["target_vi"]),
            ),
        )
    return result


def flatten_document(
    document: Mapping[str, Any], *, document_path: Path
) -> tuple[str, list[dict[str, Any]]]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list):
        raise FreezeValidationError("document.chapters: expected list")
    document_id = str(
        document.get("document_id")
        or document.get("source_id")
        or document_path.stem
    )
    rows: list[dict[str, Any]] = []
    order = 0
    for chapter_index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            raise FreezeValidationError(
                f"document.chapters[{chapter_index}]: expected object"
            )
        chapter_id = chapter.get("chapter_id")
        if not isinstance(chapter_id, str) or not chapter_id:
            raise FreezeValidationError(
                f"document.chapters[{chapter_index}].chapter_id: invalid"
            )
        blocks = chapter.get("blocks")
        if not isinstance(blocks, list):
            raise FreezeValidationError(
                f"document.chapters[{chapter_index}].blocks: expected list"
            )
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                raise FreezeValidationError("document block: expected object")
            block_id = block.get("block_id")
            source_text = block.get("source_text")
            if not isinstance(block_id, str) or not isinstance(source_text, str):
                raise FreezeValidationError("document block: invalid id/text")
            rows.append(
                {
                    "document_id": document_id,
                    "chapter_id": chapter_id,
                    "chapter_index": chapter_index,
                    "block_id": block_id,
                    "block_index": block_index,
                    "block_type": str(block.get("block_type") or "unknown"),
                    "source_text": source_text,
                    "source_text_sha256": sha256_text(source_text),
                    "order": order,
                }
            )
            order += 1
    return document_id, rows


def _surface_rows(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    pattern_to_terms: dict[str, list[str]] = defaultdict(list)
    surfaces_by_term: dict[str, list[str]] = {}
    for index, record in enumerate(records):
        value = record["value"]
        term_id = str(record.get("record_id") or value.get("entry_id") or index)
        raw_surfaces = [
            value.get("canonical_source"),
            *(value.get("surfaces") or []),
        ]
        surfaces = stable_unique(
            row for row in raw_surfaces if isinstance(row, str)
        )
        surfaces_by_term[term_id] = surfaces
        for surface in surfaces:
            normalized = normalize_text(surface)
            if normalized:
                pattern_to_terms[normalized].append(term_id)
    return dict(pattern_to_terms), surfaces_by_term


def _aho_automaton(patterns: Iterable[str]) -> tuple[list[dict[str, int]], list[int], list[list[str]]]:
    transitions: list[dict[str, int]] = [{}]
    failure = [0]
    outputs: list[list[str]] = [[]]
    for pattern in sorted(set(patterns)):
        state = 0
        for char in pattern:
            next_state = transitions[state].get(char)
            if next_state is None:
                next_state = len(transitions)
                transitions[state][char] = next_state
                transitions.append({})
                failure.append(0)
                outputs.append([])
            state = next_state
        outputs[state].append(pattern)
    queue: deque[int] = deque()
    for state in transitions[0].values():
        queue.append(state)
    while queue:
        state = queue.popleft()
        for char, next_state in transitions[state].items():
            queue.append(next_state)
            fallback = failure[state]
            while fallback and char not in transitions[fallback]:
                fallback = failure[fallback]
            failure[next_state] = transitions[fallback].get(char, 0)
            outputs[next_state].extend(outputs[failure[next_state]])
    return transitions, failure, outputs


def _is_word_char(char: str) -> bool:
    return char == "_" or char.isalnum()


def _sentence_span(text: str, start: int, end: int) -> tuple[int, int, int]:
    boundaries = [0]
    for match in re.finditer(r"(?<=[.!?])\s+", text):
        boundaries.append(match.end())
    boundaries.append(len(text))
    for index in range(len(boundaries) - 1):
        sentence_start = boundaries[index]
        sentence_end = boundaries[index + 1]
        if sentence_start <= start and end <= sentence_end:
            while sentence_start < sentence_end and text[sentence_start].isspace():
                sentence_start += 1
            while sentence_end > sentence_start and text[sentence_end - 1].isspace():
                sentence_end -= 1
            return sentence_start, sentence_end, index
    return 0, len(text), 0


def build_context_candidates(
    *,
    records: Sequence[Mapping[str, Any]],
    document_blocks: Sequence[Mapping[str, Any]],
    document_binding: Mapping[str, str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    pattern_to_terms, surfaces_by_term = _surface_rows(records)
    transitions, failure, outputs = _aho_automaton(pattern_to_terms)
    matches_by_term: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_by_term: dict[str, set[tuple[str, int, int]]] = defaultdict(set)
    seen_content_by_term: dict[str, set[str]] = defaultdict(set)
    for block in document_blocks:
        source_text = str(block["source_text"])
        folded = unicodedata.normalize("NFC", source_text).casefold()
        state = 0
        for end_index, char in enumerate(folded):
            while state and char not in transitions[state]:
                state = failure[state]
            state = transitions[state].get(char, 0)
            for pattern in outputs[state]:
                start_index = end_index - len(pattern) + 1
                before = folded[start_index - 1] if start_index else ""
                after_index = end_index + 1
                after = folded[after_index] if after_index < len(folded) else ""
                if (before and _is_word_char(before)) or (
                    after and _is_word_char(after)
                ):
                    continue
                if len(folded) != len(source_text):
                    # D2L source is English. Refuse unreliable offsets for a
                    # Unicode case-fold expansion instead of inventing ranges.
                    continue
                sentence_start, sentence_end, sentence_index = _sentence_span(
                    source_text, start_index, end_index + 1
                )
                context_text = source_text[sentence_start:sentence_end]
                if not context_text.strip():
                    continue
                content_sha256 = sha256_text(context_text)
                for term_id in pattern_to_terms[pattern]:
                    dedupe_key = (
                        str(block["block_id"]),
                        sentence_start,
                        sentence_end,
                    )
                    if (
                        dedupe_key in seen_by_term[term_id]
                        or content_sha256 in seen_content_by_term[term_id]
                    ):
                        continue
                    seen_by_term[term_id].add(dedupe_key)
                    seen_content_by_term[term_id].add(content_sha256)
                    matches_by_term[term_id].append(
                        {
                            "context_id": stable_id(
                                "ctx",
                                term_id,
                                str(block["block_id"]),
                                str(sentence_start),
                                str(sentence_end),
                            ),
                            "source_text": context_text,
                            "content_sha256": content_sha256,
                            "matched_surface": pattern,
                            "match_start": start_index,
                            "match_end": end_index + 1,
                            "provenance": {
                                "document_id": block["document_id"],
                                "chapter_id": block["chapter_id"],
                                "chapter_index": block["chapter_index"],
                                "block_id": block["block_id"],
                                "block_index": block["block_index"],
                                "sentence_id": stable_id(
                                    "sentence",
                                    str(block["block_id"]),
                                    str(sentence_index),
                                ),
                                "sentence_index": sentence_index,
                                "source_start": sentence_start,
                                "source_end": sentence_end,
                                "block_text_sha256": block[
                                    "source_text_sha256"
                                ],
                                "source_artifact_ref": document_binding["ref"],
                                "source_artifact_sha256": document_binding[
                                    "physical_sha256"
                                ],
                            },
                            "_order": block["order"],
                        }
                    )
    return dict(matches_by_term), surfaces_by_term


def select_diverse_contexts(
    rows: Sequence[Mapping[str, Any]], *, count: int
) -> list[dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda row: (
            int(row["_order"]),
            str(row["context_id"]),
        ),
    )
    selected: list[Mapping[str, Any]] = []
    selected_ids: set[str] = set()
    chapters: set[str] = set()
    for row in ordered:
        chapter_id = str(row["provenance"]["chapter_id"])
        if chapter_id in chapters:
            continue
        selected.append(row)
        selected_ids.add(str(row["context_id"]))
        chapters.add(chapter_id)
        if len(selected) == count:
            break
    for row in ordered:
        if len(selected) == count:
            break
        if str(row["context_id"]) in selected_ids:
            continue
        selected.append(row)
        selected_ids.add(str(row["context_id"]))
    return [
        {key: value for key, value in row.items() if key != "_order"}
        for row in selected
    ]


def classify_stratum(value: Mapping[str, Any]) -> str:
    resolution = value.get("resolution")
    lineage = value.get("source_lineage")
    resolution_kind = (
        resolution.get("authority_kind") if isinstance(resolution, dict) else None
    )
    lineage_kind = (
        lineage.get("authority_kind") if isinstance(lineage, dict) else None
    )
    if resolution_kind == "stage3_multi_target_audit" or lineage_kind == (
        "stage2_target_collision_audit"
    ):
        return "collision_or_multi_target"
    if (
        value.get("directive") == "contextual"
        or value.get("evidence_complete") is not True
        or len(value.get("source_member_candidate_ids") or []) > 1
        or any(value.get(key) for key in TARGET_KEYS)
    ):
        return "ambiguous"
    return "clear"


