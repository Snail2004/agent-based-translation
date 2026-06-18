from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Protocol

from pipeline.agents.llm_client import LLMClient, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig
from pipeline.eval.d2l_translate_score import (
    DEFAULT_EVAL_ROOT,
    ScopeBlock,
    _load_registry_rows,
    _load_translations,
    _prepare_eval_registry_rows,
    _resolve_chapters,
    _scope_blocks,
)
from pipeline.eval.surface_match import SurfaceOwner, allocate_spans, find_spans, normalize_surface
from pipeline.eval.term_policy import load_term_policy_assets
from pipeline.translate.profiles import get_profile


DEFAULT_DOC_ID = "d2l"
DEFAULT_EXPERIMENT_ID = "d2l_p3"
DEFAULT_PROFILE = "technical_d2l_v1"
DEFAULT_SIMALIGN_MODEL = "bert"
DEFAULT_SIMALIGN_METHOD = "itermax"
DEFAULT_ALIGN_SEED = 42


@dataclass(frozen=True)
class OccItem:
    occ_id: str
    block_id: str
    chapter_id: str
    source_term: str
    glossary_id: str
    src_start: int
    src_end: int
    src_surface: str
    sentence_en: str
    tier: str = "soft"
    accepted_forms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Proposal:
    occ_id: str
    block_id: str
    config: str
    branch: str
    source: str
    target_start: int | None
    target_end: int | None
    target_surface: str | None
    status: str
    present: bool
    instrument_error: str | None = None
    model: str | None = None
    method: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenSpan:
    text: str
    start: int
    end: int


class WordAligner(Protocol):
    def get_word_aligns(self, src_sent: list[str], trg_sent: list[str]) -> dict[str, list[Any]]:
        ...


def build_occ_frame(
    blocks: Iterable[ScopeBlock],
    terms: Iterable[dict[str, Any]],
    *,
    chapter: str,
) -> list[OccItem]:
    """List source-side registry occurrences with stable ids.

    The frame is source-only and reused for every config and branch. Target-side
    tools may fail differently, but they all operate over the same denominator.
    """

    term_rows = [_term_row(row) for row in terms if str(row.get("source_term") or "").strip()]
    owners = [
        SurfaceOwner(row["owner_id"], row["source_term"], row["case_sensitive"])
        for row in term_rows
    ]
    row_by_owner = {row["owner_id"]: row for row in term_rows}
    result: list[OccItem] = []
    for block in blocks:
        if block.chapter_id != chapter:
            continue
        allocated = allocate_spans(block.text, owners, language="en")
        for owner_id, spans in allocated.items():
            row = row_by_owner[owner_id]
            for span in spans:
                occ_id = _occ_id(block.block_id, span.start, span.end, row["source_term"])
                result.append(
                    OccItem(
                        occ_id=occ_id,
                        block_id=block.block_id,
                        chapter_id=block.chapter_id,
                        source_term=row["source_term"],
                        glossary_id=row["glossary_id"],
                        src_start=span.start,
                        src_end=span.end,
                        src_surface=span.surface,
                        sentence_en=_sentence_for_span(block.text, span.start, span.end),
                        tier=row["tier"],
                        accepted_forms=tuple(row["accepted_forms"]),
                    )
                )
    return sorted(result, key=lambda item: (item.block_id, item.src_start, item.src_end, item.source_term))


def load_occ_inputs(
    db_path: str | Path,
    *,
    chapter: str,
    doc_id: str = DEFAULT_DOC_ID,
    profile_name: str = DEFAULT_PROFILE,
    term_policy_root: str | Path | None = None,
) -> tuple[str, list[ScopeBlock], list[OccItem]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        resolved = _resolve_chapters(conn, doc_id, [chapter])[0]
        profile = get_profile(profile_name)
        blocks = _scope_blocks(conn, doc_id, [resolved], profile)
        registry_rows = _load_registry_rows(conn, doc_id)
        policy_assets = load_term_policy_assets(term_policy_root or DEFAULT_EVAL_ROOT)
        eval_rows = _prepare_eval_registry_rows(registry_rows, policy_assets)
        occ_frame = build_occ_frame(blocks, eval_rows, chapter=resolved)
        return resolved, blocks, occ_frame
    finally:
        conn.close()


def load_frozen_translations(
    db_path: str | Path,
    *,
    config: str,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _load_translations(conn, experiment_id, config)
    finally:
        conn.close()


def load_translation_model(
    db_path: str | Path,
    *,
    config: str,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT model
            FROM translation_runs
            WHERE experiment_id = ? AND config = ? AND stage = 'draft'
              AND COALESCE(model, '') != ''
            ORDER BY model
            """,
            (experiment_id, config),
        ).fetchall()
    finally:
        conn.close()
    models = [str(row["model"]) for row in rows]
    if not models:
        raise ValueError(f"No translation model recorded for config={config}")
    if len(models) > 1:
        raise ValueError(f"Multiple translation models recorded for config={config}: {models}")
    return models[0]


def align_independent(
    occ_frame: list[OccItem],
    blocks_by_id: dict[str, str],
    frozen_targets: dict[str, str],
    *,
    config: str,
    aligner: WordAligner,
    align_method: str = DEFAULT_SIMALIGN_METHOD,
    model: str = DEFAULT_SIMALIGN_MODEL,
    seed: int = DEFAULT_ALIGN_SEED,
) -> list[Proposal]:
    random.seed(seed)
    proposals: list[Proposal] = []
    occ_by_block: dict[str, list[OccItem]] = defaultdict(list)
    for occ in occ_frame:
        occ_by_block[occ.block_id].append(occ)

    for block_id in sorted(occ_by_block):
        source_text = blocks_by_id.get(block_id, "")
        target_text = frozen_targets.get(block_id, "")
        source_tokens = tokenize_with_spans(source_text)
        target_tokens = tokenize_with_spans(target_text)
        if not source_tokens or not target_tokens:
            for occ in occ_by_block[block_id]:
                proposals.append(_missing_proposal(occ, config, "simalign", model, align_method))
            continue
        aligns = aligner.get_word_aligns(
            [token.text for token in source_tokens],
            [token.text for token in target_tokens],
        )
        pairs = _alignment_pairs(aligns, align_method)
        by_source: dict[int, set[int]] = defaultdict(set)
        for src_index, trg_index in pairs:
            if 0 <= src_index < len(source_tokens) and 0 <= trg_index < len(target_tokens):
                by_source[src_index].add(trg_index)

        for occ in occ_by_block[block_id]:
            source_indexes = [
                index
                for index, token in enumerate(source_tokens)
                if _overlaps(token.start, token.end, occ.src_start, occ.src_end)
            ]
            target_indexes = sorted({target for index in source_indexes for target in by_source.get(index, set())})
            if not target_indexes:
                proposals.append(_missing_proposal(occ, config, "simalign", model, align_method))
                continue
            start = min(target_tokens[index].start for index in target_indexes)
            end = max(target_tokens[index].end for index in target_indexes)
            surface = target_text[start:end]
            proposals.append(
                Proposal(
                    occ_id=occ.occ_id,
                    block_id=occ.block_id,
                    config=config,
                    branch="simalign",
                    source="independent",
                    target_start=start,
                    target_end=end,
                    target_surface=surface,
                    status="aligned",
                    present=True,
                    model=model,
                    method=align_method,
                    metadata={"source_token_indexes": source_indexes, "target_token_indexes": target_indexes},
                )
            )
    return sorted(proposals, key=lambda item: (item.block_id, item.occ_id, item.config, item.branch))


def verify_presence(frozen_target: str, proposal: Proposal) -> Proposal:
    if proposal.target_start is None or proposal.target_end is None:
        return proposal
    surface = proposal.target_surface or ""
    if frozen_target[proposal.target_start:proposal.target_end] == surface:
        return proposal
    matches = find_spans(frozen_target, surface, language="vi")
    for start, end, _ in matches:
        if start == proposal.target_start and end == proposal.target_end:
            return proposal
    return Proposal(
        **{
            **asdict(proposal),
            "present": False,
            "status": "instrument_error",
            "instrument_error": "claimed surface is not present at claimed offset",
        }
    )


def render_selfreport_messages(
    occ_items: list[OccItem],
    *,
    source_text: str,
    frozen_target: str,
    config: str,
) -> list[dict[str, str]]:
    marked_source = _mark_occurrences(source_text, occ_items)
    system = (
        "You are auditing a frozen English-to-Vietnamese translation after the fact. "
        "You do not translate. You only identify which exact Vietnamese surface span, "
        "if any, renders each marked English occurrence. Return JSON only."
    )
    user = {
        "task": "Map each OCC marker in SOURCE to an exact substring in TARGET.",
        "rules": [
            "Use only TARGET text that already exists verbatim.",
            "If no exact rendering exists, use status NOT_RENDERED.",
            "If one Vietnamese phrase renders multiple source occurrences together, use status FUSED and quote that phrase.",
            "Do not use or infer any glossary. This prompt is the same for S0 and S1.",
        ],
        "config": config,
        "source_with_occ_markers": marked_source,
        "target_frozen_translation": frozen_target,
        "occurrences": [
            {
                "occ_id": item.occ_id,
                "source_term": item.source_term,
                "source_surface": item.src_surface,
            }
            for item in occ_items
        ],
        "json_schema": {
            "items": [
                {
                    "occ_id": "string",
                    "status": "ALIGNED|NOT_RENDERED|FUSED",
                    "target_surface": "exact substring or empty",
                }
            ]
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False, indent=2)},
    ]


def preview_selfreport(
    occ_frame: list[OccItem],
    blocks_by_id: dict[str, str],
    frozen_targets: dict[str, str],
    *,
    config: str,
    llm_config: LLMConfig,
) -> dict[str, Any]:
    messages = _selfreport_messages_by_block(occ_frame, blocks_by_id, frozen_targets, config=config)
    prompt_tokens = sum(
        estimate_prompt_tokens(
            block_messages,
            response_format={"type": "json_object"},
        )
        for block_messages in messages.values()
    )
    max_output = len(messages) * llm_config.max_output_tokens
    uncached_cost = (
        (prompt_tokens / 1_000_000) * llm_config.pricing["input"]
        + (max_output / 1_000_000) * llm_config.pricing["output"]
    )
    token = confirm_token("selfreport", config, llm_config.model, len(messages), prompt_tokens, max_output)
    return {
        "method": "selfreport",
        "config": config,
        "model": llm_config.model,
        "calls": len(messages),
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_max_output_tokens": max_output,
        "estimated_uncached_cost_usd": round(uncached_cost, 6),
        "confirm_token": token,
    }


def align_selfreport(
    occ_frame: list[OccItem],
    blocks_by_id: dict[str, str],
    frozen_targets: dict[str, str],
    *,
    config: str,
    client: LLMClient,
) -> list[Proposal]:
    messages_by_block = _selfreport_messages_by_block(occ_frame, blocks_by_id, frozen_targets, config=config)
    occ_by_id = {item.occ_id: item for item in occ_frame}
    proposals: list[Proposal] = []
    response_format = {"type": "json_object"}
    for block_id, messages in messages_by_block.items():
        result = client.call(messages, response_format=response_format, tag=f"occ_align_selfreport:{config}:{block_id}")
        if result.json_error:
            raise ValueError(f"Self-report JSON parse failed for {block_id}: {result.json_error}")
        parsed = result.parsed_json or {}
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            raise ValueError(f"Self-report response missing items list for {block_id}")
        target = frozen_targets.get(block_id, "")
        for row in items:
            if not isinstance(row, dict):
                continue
            occ_id = str(row.get("occ_id") or "")
            occ = occ_by_id.get(occ_id)
            if occ is None:
                continue
            status = str(row.get("status") or "NOT_RENDERED").upper()
            surface = str(row.get("target_surface") or "").strip()
            if status == "NOT_RENDERED" or not surface:
                proposal = Proposal(
                    occ_id=occ.occ_id,
                    block_id=occ.block_id,
                    config=config,
                    branch="selfreport",
                    source="selfreport",
                    target_start=None,
                    target_end=None,
                    target_surface=None,
                    status="not_rendered",
                    present=True,
                    model=client.config.model,
                    method="selfreport",
                )
            else:
                spans = find_spans(target, surface, language="vi")
                if not spans:
                    proposal = Proposal(
                        occ_id=occ.occ_id,
                        block_id=occ.block_id,
                        config=config,
                        branch="selfreport",
                        source="selfreport",
                        target_start=None,
                        target_end=None,
                        target_surface=surface,
                        status="instrument_error",
                        present=False,
                        instrument_error="claimed surface is not present in frozen output",
                        model=client.config.model,
                        method="selfreport",
                    )
                else:
                    start, end, found = spans[0]
                    proposal = Proposal(
                        occ_id=occ.occ_id,
                        block_id=occ.block_id,
                        config=config,
                        branch="selfreport",
                        source="selfreport",
                        target_start=start,
                        target_end=end,
                        target_surface=found,
                        status="fused" if status == "FUSED" else "aligned",
                        present=True,
                        model=client.config.model,
                        method="selfreport",
                    )
            proposals.append(verify_presence(target, proposal))
    return sorted(proposals, key=lambda item: (item.block_id, item.occ_id, item.config, item.branch))


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any] | Proposal]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            payload = asdict(row) if isinstance(row, Proposal) else dict(row)
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def simalign_cached(
    *,
    cache_dir: str | Path,
    cache_key: str,
    compute: Any,
) -> list[dict[str, Any]]:
    cache_path = Path(cache_dir) / f"{cache_key}.jsonl"
    if cache_path.exists():
        return read_jsonl(cache_path)
    rows = [asdict(item) if isinstance(item, Proposal) else dict(item) for item in compute()]
    write_jsonl(cache_path, rows)
    return rows


def simalign_cache_key(
    *,
    model: str,
    method: str,
    seed: int,
    block_ids: Iterable[str],
    config: str,
    source_hash: str,
    target_hash: str,
) -> str:
    payload = {
        "tool": "simalign",
        "tool_version": _package_version("simalign"),
        "model": model,
        "method": method,
        "seed": seed,
        "block_ids": sorted(block_ids),
        "config": config,
        "source_hash": source_hash,
        "target_hash": target_hash,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def make_simalign_aligner(*, model: str = DEFAULT_SIMALIGN_MODEL, device: str = "cpu") -> WordAligner:
    try:
        from simalign import SentenceAligner
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("simalign==0.4 is required for --method simalign") from exc
    return SentenceAligner(model=model, token_type="bpe", matching_methods="mai", device=device)


def build_gold_sample_rows(
    occ_frame: list[OccItem],
    translations_by_config: dict[str, dict[str, str]],
    report: dict[str, Any],
    *,
    cap_per_term: int,
    seed: int,
    max_rows: int = 60,
) -> list[dict[str, Any]]:
    status_lookup = _status_lookup(report)
    candidates: list[dict[str, Any]] = []
    for config, translations in sorted(translations_by_config.items()):
        per_term_counts: Counter[str] = Counter()
        for occ in occ_frame:
            status = status_lookup.get((config, occ.source_term), "unknown")
            candidates.append(
                {
                    "sample_id": f"{config}:{occ.occ_id}",
                    "config": config,
                    "occ_id": occ.occ_id,
                    "block_id": occ.block_id,
                    "src_term": occ.source_term,
                    "status": status,
                    "tier": occ.tier,
                    "sentence_EN": occ.sentence_en,
                    "target_output_VI": translations.get(occ.block_id, ""),
                    "accepted_forms": " | ".join(occ.accepted_forms),
                    "gold_target_start": "",
                    "gold_target_end": "",
                    "gold_surface": "",
                    "annotator": "",
                    "note": "",
                }
            )
            per_term_counts[occ.source_term] += 1

    rng = random.Random(seed)
    strata: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        strata[(row["config"], row["tier"], row["status"])].append(row)

    selected: list[dict[str, Any]] = []
    term_counts_by_config: Counter[tuple[str, str]] = Counter()
    shuffled: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for key, rows in strata.items():
        items = list(rows)
        rng.shuffle(items)
        shuffled[key] = items
    keys = sorted(shuffled)
    while len(selected) < max_rows and any(shuffled.values()):
        progressed = False
        for key in keys:
            rows = shuffled[key]
            while rows:
                row = rows.pop(0)
                term_key = (row["config"], row["src_term"])
                if term_counts_by_config[term_key] >= cap_per_term:
                    continue
                selected.append(row)
                term_counts_by_config[term_key] += 1
                progressed = True
                break
            if len(selected) >= max_rows:
                break
        if not progressed:
            break
    return sorted(selected, key=lambda row: (row["config"], row["block_id"], row["occ_id"]))


def write_gold_sample_csv(
    path: str | Path,
    rows: list[dict[str, Any]],
    *,
    seed: int,
    cap_per_term: int,
    max_rows: int,
) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "sample_id",
        "config",
        "occ_id",
        "block_id",
        "src_term",
        "status",
        "tier",
        "sentence_EN",
        "target_output_VI",
        "accepted_forms",
        "gold_target_start",
        "gold_target_end",
        "gold_surface",
        "annotator",
        "note",
    ]
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write(
            f"# provenance: seed={seed}; cap_per_term={cap_per_term}; "
            f"max_rows={max_rows}; proposer_blind=true\n"
        )
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def audit_occ_align(
    proposal_paths: list[str | Path],
    gold_path: str | Path,
    *,
    out_path: str | Path,
) -> dict[str, Any]:
    proposals = _load_proposals(proposal_paths)
    gold_rows = _load_gold_rows(gold_path)
    final_gold, iaa = _finalize_gold(gold_rows)
    per_branch: dict[str, Any] = {}
    for branch in sorted({key[0] for key in proposals}):
        per_branch[branch] = _score_branch(branch, proposals, final_gold)
    single = bool(iaa["single_annotator"])
    gate = _gate(per_branch, single_annotator=single)
    blindspot = _blindspot(final_gold)
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "metric_version": "occ_align_pilot_v1",
        "per_branch": per_branch,
        "iaa": iaa,
        "blindspot": blindspot,
        "gate": gate,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def confirm_token(method: str, config: str, model: str, calls: int, prompt_tokens: int, output_tokens: int) -> str:
    payload = f"{method}|{config}|{model}|{calls}|{prompt_tokens}|{output_tokens}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def tokenize_with_spans(text: str) -> list[TokenSpan]:
    value = str(text or "")
    return [
        TokenSpan(match.group(0), match.start(), match.end())
        for match in re.finditer(r"\w+(?:[-.]\w+)*|[^\w\s]", value, flags=re.UNICODE)
    ]


def _term_row(row: dict[str, Any]) -> dict[str, Any]:
    source = str(row.get("source_term") or "").strip()
    glossary_id = str(row.get("glossary_id") or source)
    accepted = [str(row.get("target_term") or "").strip()]
    accepted.extend(str(item).strip() for item in _json_list(row.get("allowed_variants_json")))
    accepted = [item for item in dict.fromkeys(item for item in accepted if item)]
    return {
        "owner_id": glossary_id,
        "glossary_id": glossary_id,
        "source_term": source,
        "case_sensitive": bool(int(row.get("case_sensitive") or 0)),
        "tier": str(row.get("constraint_strength") or "soft"),
        "accepted_forms": accepted,
    }


def _occ_id(block_id: str, start: int, end: int, source_term: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", source_term.strip().lower()).strip("_")[:48]
    return f"{block_id}:{start}:{end}:{slug}"


def _sentence_for_span(text: str, start: int, end: int) -> str:
    value = str(text or "")
    left_candidates = [value.rfind(mark, 0, start) for mark in [".", "?", "!", "\n"]]
    left = max(left_candidates)
    right_candidates = [value.find(mark, end) for mark in [".", "?", "!", "\n"]]
    right = min([item for item in right_candidates if item >= 0], default=len(value))
    return value[left + 1:right + 1].strip() or value.strip()


def _missing_proposal(
    occ: OccItem,
    config: str,
    branch: str,
    model: str,
    method: str,
) -> Proposal:
    return Proposal(
        occ_id=occ.occ_id,
        block_id=occ.block_id,
        config=config,
        branch=branch,
        source="independent" if branch == "simalign" else branch,
        target_start=None,
        target_end=None,
        target_surface=None,
        status="missing_alignment",
        present=True,
        model=model,
        method=method,
    )


def _alignment_pairs(aligns: dict[str, list[Any]], method: str) -> list[tuple[int, int]]:
    raw_pairs = aligns.get(method)
    if raw_pairs is None:
        raw_pairs = aligns.get(method.replace("_", "-"))
    if raw_pairs is None and method == "itermax":
        raw_pairs = aligns.get("itermax")
    if raw_pairs is None:
        raise ValueError(f"Alignment method {method!r} not found in {sorted(aligns)}")
    result: list[tuple[int, int]] = []
    for pair in raw_pairs:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            result.append((int(pair[0]), int(pair[1])))
    return sorted(set(result))


def _overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def _selfreport_messages_by_block(
    occ_frame: list[OccItem],
    blocks_by_id: dict[str, str],
    frozen_targets: dict[str, str],
    *,
    config: str,
) -> dict[str, list[dict[str, str]]]:
    occ_by_block: dict[str, list[OccItem]] = defaultdict(list)
    for occ in occ_frame:
        occ_by_block[occ.block_id].append(occ)
    return {
        block_id: render_selfreport_messages(
            items,
            source_text=blocks_by_id.get(block_id, ""),
            frozen_target=frozen_targets.get(block_id, ""),
            config=config,
        )
        for block_id, items in sorted(occ_by_block.items())
        if frozen_targets.get(block_id, "")
    }


def _mark_occurrences(source_text: str, occ_items: list[OccItem]) -> str:
    value = str(source_text or "")
    pieces: list[str] = []
    cursor = 0
    for occ in sorted(occ_items, key=lambda item: (item.src_start, item.src_end)):
        pieces.append(value[cursor:occ.src_start])
        pieces.append(f"[[OCC:{occ.occ_id}]]")
        pieces.append(value[occ.src_start:occ.src_end])
        pieces.append("[[/OCC]]")
        cursor = occ.src_end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_texts(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def hashes_for_blocks(blocks_by_id: dict[str, str], translations: dict[str, str]) -> tuple[str, str]:
    block_ids = sorted(blocks_by_id)
    return (
        _hash_texts(blocks_by_id[block_id] for block_id in block_ids),
        _hash_texts(translations.get(block_id, "") for block_id in block_ids),
    )


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unavailable"


def _status_lookup(report: dict[str, Any]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    section = report.get("D_registry_consistency") or {}
    for config, payload in section.items():
        for item in payload.get("terms_all") or []:
            result[(str(config), str(item.get("source_term") or ""))] = str(item.get("status") or "unknown")
    return result


def _load_proposals(paths: list[str | Path]) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            branch = str(row.get("branch") or row.get("source") or Path(path).stem)
            config = str(row.get("config") or "")
            occ_id = str(row.get("occ_id") or "")
            if branch and config and occ_id:
                result[(branch, config, occ_id)] = row
    return result


def _load_gold_rows(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        filtered = (line for line in fh if not line.startswith("#"))
        for row in csv.DictReader(filtered):
            if row.get("occ_id"):
                rows.append(row)
    return rows


def _gold_label(row: dict[str, Any]) -> tuple[int | None, int | None, str]:
    start_raw = str(row.get("gold_target_start") or "").strip()
    end_raw = str(row.get("gold_target_end") or "").strip()
    surface = str(row.get("gold_surface") or "").strip()
    if not start_raw and not end_raw and not surface:
        return (None, None, "")
    return (int(start_raw), int(end_raw), normalize_surface(surface))


def _finalize_gold(rows: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    annotators: set[str] = set()
    for row in rows:
        key = (str(row.get("config") or ""), str(row.get("occ_id") or ""))
        grouped[key].append(row)
        annotator = str(row.get("annotator") or "").strip()
        if annotator:
            annotators.add(annotator)
    final: dict[tuple[str, str], dict[str, Any]] = {}
    for key, items in grouped.items():
        labels = [_gold_label(row) for row in items]
        label = Counter(labels).most_common(1)[0][0]
        base = dict(items[0])
        base["gold_target_start"], base["gold_target_end"], base["gold_surface_norm"] = label
        final[key] = base
    kappa = _cohen_kappa(grouped) if len(annotators) >= 2 else None
    return final, {
        "kappa": kappa,
        "n_annotators": len(annotators) or 1,
        "single_annotator": len(annotators) < 2,
    }


def _cohen_kappa(grouped: dict[tuple[str, str], list[dict[str, Any]]]) -> float | None:
    pairs: list[tuple[tuple[int | None, int | None, str], tuple[int | None, int | None, str]]] = []
    for items in grouped.values():
        by_annotator: dict[str, dict[str, Any]] = {}
        for row in items:
            annotator = str(row.get("annotator") or "").strip()
            if annotator:
                by_annotator.setdefault(annotator, row)
        if len(by_annotator) < 2:
            continue
        first_two = [by_annotator[key] for key in sorted(by_annotator)[:2]]
        pairs.append((_gold_label(first_two[0]), _gold_label(first_two[1])))
    if not pairs:
        return None
    observed = sum(1 for left, right in pairs if left == right) / len(pairs)
    labels = [label for pair in pairs for label in pair]
    counts = Counter(labels)
    expected = sum((count / len(labels)) ** 2 for count in counts.values())
    if expected == 1:
        return 1.0
    return round((observed - expected) / (1 - expected), 6)


def _score_branch(
    branch: str,
    proposals: dict[tuple[str, str, str], dict[str, Any]],
    gold: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    by_config: dict[str, dict[str, Any]] = {}
    for config in sorted({key[0] for key in gold}):
        items = [(key, row) for key, row in gold.items() if key[0] == config]
        correct = 0
        hallucination = 0
        miss = 0
        for (cfg, occ_id), gold_row in items:
            proposal = proposals.get((branch, cfg, occ_id))
            label = (
                gold_row.get("gold_target_start"),
                gold_row.get("gold_target_end"),
                str(gold_row.get("gold_surface_norm") or ""),
            )
            if proposal is None:
                miss += 1
                continue
            if proposal.get("present") is False or proposal.get("status") == "instrument_error":
                hallucination += 1
            pred = (
                proposal.get("target_start"),
                proposal.get("target_end"),
                normalize_surface(str(proposal.get("target_surface") or "")),
            )
            if _proposal_matches_gold(pred, label):
                correct += 1
        total = len(items)
        by_config[config] = {
            "accuracy": round(correct / total, 6) if total else 0.0,
            "correct": correct,
            "n": total,
            "hallucination_rate": round(hallucination / total, 6) if total else 0.0,
            "miss_rate": round(miss / total, 6) if total else 0.0,
        }
    acc_s0 = by_config.get("S0", {}).get("accuracy", 0.0)
    acc_s1 = by_config.get("S1", {}).get("accuracy", 0.0)
    return {
        "acc_S0": acc_s0,
        "acc_S1": acc_s1,
        "differential": round(abs(float(acc_s1) - float(acc_s0)), 6),
        "by_config": by_config,
    }


def _proposal_matches_gold(
    pred: tuple[Any, Any, str],
    gold: tuple[Any, Any, str],
) -> bool:
    pred_start, pred_end, pred_surface = pred
    gold_start, gold_end, gold_surface = gold
    pred_none = pred_start is None and pred_end is None and not pred_surface
    gold_none = gold_start is None and gold_end is None and not gold_surface
    if pred_none or gold_none:
        return pred_none and gold_none
    return int(pred_start) == int(gold_start) and int(pred_end) == int(gold_end)


def _gate(per_branch: dict[str, Any], *, single_annotator: bool) -> dict[str, Any]:
    if single_annotator:
        return {
            "decision": "diagnostic_only",
            "headline_branch": None,
            "crosscheck_branch": None,
            "rationale": "single_annotator=true blocks occurrence-level headline",
        }
    qualified: list[tuple[str, float]] = []
    for branch, stats in per_branch.items():
        minimum = min(float(stats.get("acc_S0", 0.0)), float(stats.get("acc_S1", 0.0)))
        differential = float(stats.get("differential", 1.0))
        if minimum >= 0.9 and differential <= 0.03:
            qualified.append((branch, minimum))
    if not qualified:
        return {
            "decision": "diagnostic_only",
            "headline_branch": None,
            "crosscheck_branch": None,
            "rationale": "no branch met min(acc_S0,acc_S1)>=0.90 and differential<=0.03",
        }
    qualified.sort(key=lambda item: (-item[1], item[0]))
    headline = qualified[0][0]
    crosscheck = qualified[1][0] if len(qualified) > 1 else None
    return {
        "decision": "headline_ready",
        "headline_branch": headline,
        "crosscheck_branch": crosscheck,
        "rationale": "a-priori L4 gate passed",
    }


def _blindspot(gold: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    missed: list[dict[str, Any]] = []
    for (_, occ_id), row in sorted(gold.items()):
        surface = str(row.get("gold_surface") or "").strip()
        accepted = [item.strip() for item in str(row.get("accepted_forms") or "").split("|") if item.strip()]
        if surface and accepted and _norm(surface) not in {_norm(item) for item in accepted}:
            missed.append({
                "config": row.get("config"),
                "occ_id": occ_id,
                "src_term": row.get("src_term"),
                "gold_surface": surface,
                "accepted_forms": accepted,
            })
    return {
        "registry_missed_occ": len(missed),
        "examples": missed[:20],
    }


def _norm(text: str) -> str:
    return normalize_surface(text).casefold().strip()
