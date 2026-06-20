from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from pipeline.agents.llm_client import LLMClient, LLMResult, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig
from pipeline.eval.localizer import read_gold_csv, run_localizers
from pipeline.eval.surface_match import SurfaceOwner, allocate_spans, normalize_surface


PROMPT_VERSION = "d2l_localizer_t2_v2"
VALIDATOR_VERSION = "position_reanchor_v2"
DEFAULT_WINDOW_CHARS = 700
RESULT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "localized_occurrence",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "occurrence_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["localized", "omitted", "ambiguous", "not_found"],
                },
                "target_quote": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            "required": ["occurrence_id", "status", "target_quote", "start", "end"],
        },
    },
}


@dataclass(frozen=True)
class RegistryEntry:
    glossary_id: str
    source_term: str
    target_term: str
    scope: str
    chapter_id: str | None
    case_sensitive: bool
    allowed: tuple[str, ...]
    forbidden: tuple[str, ...]


@dataclass(frozen=True)
class CascadeCase:
    row_id: str
    opaque_id: str
    item_id: str
    config: str
    block_id: str
    chapter_id: str
    source_term: str
    source_text: str
    source_start: int
    source_end: int
    target_text: str
    gold_start: int | None = None
    gold_end: int | None = None
    gold_quote: str = ""


@dataclass(frozen=True)
class TargetWindow:
    text: str
    start: int
    end: int
    source_ratio: float
    level: str


@dataclass(frozen=True)
class T1Decision:
    occurrence_id: str
    status: str
    classification: str | None = None
    quote: str = ""
    start: int | None = None
    end: int | None = None
    matched_form: str = ""
    reason: str = ""


@dataclass(frozen=True)
class T2Decision:
    occurrence_id: str
    status: str
    classification: str | None
    quote: str
    start: int | None
    end: int | None
    offset_source: str
    reason: str
    from_cache: bool = False
    api_cache_hit: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    system_fingerprint: str | None = None


class LocalizationResultCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS localization_results (
                    result_key TEXT PRIMARY KEY,
                    occurrence_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, key: str) -> T2Decision | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM localization_results WHERE result_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return replace(T2Decision(**json.loads(str(row["payload_json"]))), from_cache=True)

    def put(self, key: str, value: T2Decision) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO localization_results(result_key, occurrence_id, payload_json) VALUES (?, ?, ?)",
                (key, value.occurrence_id, json.dumps(asdict(value), ensure_ascii=False, sort_keys=True)),
            )


def load_registry(db_path: str | Path, *, doc_id: str = "d2l") -> dict[str, list[RegistryEntry]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT glossary_id, source_term, target_term, scope, chapter_id,
                   case_sensitive, allowed_variants_json, forbidden_variants_json
            FROM glossary_entries WHERE doc_id = ? ORDER BY source_term, glossary_id
            """,
            (doc_id,),
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, list[RegistryEntry]] = defaultdict(list)
    for row in rows:
        entry = RegistryEntry(
            glossary_id=str(row["glossary_id"]),
            source_term=str(row["source_term"]),
            target_term=str(row["target_term"]),
            scope=str(row["scope"] or "global"),
            chapter_id=str(row["chapter_id"]) if row["chapter_id"] is not None else None,
            case_sensitive=bool(row["case_sensitive"]),
            allowed=tuple(_json_strings(row["allowed_variants_json"])),
            forbidden=tuple(_json_strings(row["forbidden_variants_json"])),
        )
        result[_key(entry.source_term)].append(entry)
    return dict(result)


def resolve_registry_entry(
    registry: dict[str, list[RegistryEntry]], source_term: str, chapter_id: str
) -> tuple[RegistryEntry | None, str]:
    entries = registry.get(_key(source_term), [])
    chapter = [entry for entry in entries if entry.chapter_id == chapter_id]
    if len(chapter) == 1:
        return chapter[0], "chapter"
    if len(chapter) > 1:
        return None, "multiple_chapter_entries"
    global_entries = [entry for entry in entries if entry.scope == "global" and entry.chapter_id is None]
    if len(global_entries) == 1:
        return global_entries[0], "global"
    if len(global_entries) > 1:
        return None, "multiple_global_entries"
    return None, "missing_registry_entry"


def load_dev_cases(
    gold_path: str | Path, db_path: str | Path
) -> tuple[list[dict[str, Any]], list[CascadeCase]]:
    rows = read_gold_csv(gold_path)
    conn = sqlite3.connect(db_path)
    try:
        chapters = {
            str(block_id): str(chapter_id)
            for block_id, chapter_id in conn.execute("SELECT block_id, chapter_id FROM blocks")
        }
    finally:
        conn.close()
    cases: list[CascadeCase] = []
    for row in rows:
        cases.append(
            CascadeCase(
                row_id=str(row["row_id"]),
                opaque_id=_opaque_id(str(row["row_id"])),
                item_id=str(row["item_id"]),
                config=str(row["config"]),
                block_id=str(row["block_id"]),
                chapter_id=chapters.get(str(row["block_id"]), ""),
                source_term=str(row["source_term"]),
                source_text=str(row["source_text"]),
                source_start=int(row["source_start"]),
                source_end=int(row["source_end"]),
                target_text=str(row["target_text"]),
                gold_start=_int_or_none(row.get("gold_target_start")),
                gold_end=_int_or_none(row.get("gold_target_end")),
                gold_quote=str(row.get("gold_target_span") or ""),
            )
        )
    return rows, cases


def legacy_longest_failures(rows: list[dict[str, Any]]) -> list[str]:
    proposals = run_localizers(rows)["longest_match"]
    failures: list[str] = []
    for row in rows:
        if str(row.get("registry_class")) != "in":
            continue
        proposal = proposals.get(str(row["row_id"]))
        start = _int_or_none(row.get("gold_target_start"))
        end = _int_or_none(row.get("gold_target_end"))
        if proposal is None or proposal.start != start or proposal.end != end:
            failures.append(str(row["row_id"]))
    return failures


def registry_conflict_count(registry: dict[str, list[RegistryEntry]]) -> int:
    return sum(
        bool(_form_classes(entry)[1])
        for entries in registry.values()
        for entry in entries
    )


def t1_localize(case: CascadeCase, entry: RegistryEntry | None, resolution: str) -> T1Decision:
    if entry is None:
        return T1Decision(case.opaque_id, "residual", reason=resolution)
    form_classes, conflicts = _form_classes(entry)
    owners = [SurfaceOwner(form_key, form) for form_key, (_, form) in form_classes.items()]
    allocated = allocate_spans(case.target_text, owners, language="vi")
    spans: list[tuple[int, int, str, str]] = []
    for form_key, owner_spans in allocated.items():
        classification, form = form_classes[form_key]
        for span in owner_spans:
            spans.append((span.start, span.end, span.surface, classification))
    spans.sort(key=lambda item: (item[0], item[1], item[2]))
    if not spans:
        return T1Decision(case.opaque_id, "residual", reason="none")
    if len(spans) != 1:
        return T1Decision(case.opaque_id, "residual", reason="multiple")
    start, end, quote, classification = spans[0]
    normalized = _key(quote)
    if normalized in conflicts:
        return T1Decision(
            case.opaque_id, "residual", quote=quote, start=start, end=end,
            matched_form=quote, reason="registry_conflict",
        )
    if _is_short_known_form(normalized, form_classes):
        return T1Decision(
            case.opaque_id, "residual", quote=quote, start=start, end=end,
            matched_form=quote, reason="short_known_form",
        )
    return T1Decision(
        case.opaque_id, "resolved", classification=classification, quote=quote,
        start=start, end=end, matched_form=quote, reason=resolution,
    )


def run_t1(
    cases: Iterable[CascadeCase], registry: dict[str, list[RegistryEntry]]
) -> dict[str, T1Decision]:
    case_list = list(cases)
    decisions: dict[str, T1Decision] = {}
    for case in case_list:
        entry, resolution = resolve_registry_entry(registry, case.source_term, case.chapter_id)
        decisions[case.opaque_id] = t1_localize(case, entry, resolution)
    claims: dict[tuple[str, str, int, int], list[str]] = defaultdict(list)
    for case in case_list:
        decision = decisions[case.opaque_id]
        if decision.status == "resolved" and decision.start is not None and decision.end is not None:
            claims[(case.config, case.block_id, decision.start, decision.end)].append(case.opaque_id)
    for occurrence_ids in claims.values():
        if len(occurrence_ids) <= 1:
            continue
        for occurrence_id in occurrence_ids:
            decisions[occurrence_id] = replace(
                decisions[occurrence_id], status="residual", classification=None,
                reason="double_claim",
            )
    return decisions


def target_window(case: CascadeCase, *, max_chars: int = DEFAULT_WINDOW_CHARS) -> TargetWindow:
    if not case.source_text or not case.target_text:
        return TargetWindow(case.target_text, 0, len(case.target_text), 0.5, "full")
    source_center = (case.source_start + case.source_end) / 2
    ratio = min(1.0, max(0.0, source_center / len(case.source_text)))
    if len(case.target_text) <= max_chars:
        return TargetWindow(case.target_text, 0, len(case.target_text), ratio, "full")
    target_center = round(ratio * len(case.target_text))
    start = max(0, target_center - max_chars // 2)
    end = min(len(case.target_text), start + max_chars)
    start = max(0, end - max_chars)
    return TargetWindow(case.target_text[start:end], start, end, ratio, "position")


def build_t2_messages(case: CascadeCase, window: TargetWindow) -> list[dict[str, str]]:
    marked = _marked_source_sentence(case)
    system = (
        "You are a bilingual occurrence span localizer, not a translator or quality judge. "
        "Only the English words inside TERM_START/TERM_END belong to the concept you must localize; "
        "never return the translation of an adjacent unmarked alternative or neighbor. "
        "Locate the smallest contiguous Vietnamese phrase that fully renders every content word inside the markers. "
        "Do not return only an acronym or proper name when the marked source also contains a common noun. "
        "Include grammatical or classifier words required to express the marked concept, but exclude framing, "
        "quantifiers, modifiers, prepositions, and complements that translate words outside the markers. "
        "Return an exact quote copied from TARGET_WINDOW. Never rewrite or normalize it. "
        "Offsets are zero-based character offsets relative to TARGET_WINDOW. "
        "If the rendering is omitted or cannot be uniquely identified, use omitted, ambiguous, or not_found."
    )
    payload = {
        "prompt_version": PROMPT_VERSION,
        "occurrence_id": case.opaque_id,
        "source_context": marked,
        "target_window": window.text,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def result_cache_key(case: CascadeCase, window: TargetWindow, config: LLMConfig) -> str:
    payload = {
        "model": config.model,
        "temperature": config.temperature,
        "seed": config.seed,
        "reasoning_effort": config.reasoning_effort,
        "max_output_tokens": config.max_output_tokens,
        "prompt_version": PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "config": case.config,
        "block_hash": _hash(case.source_text + "\n" + case.target_text),
        "occurrence_id": case.opaque_id,
        "source_occurrence_hash": _hash(
            f"{case.source_start}:{case.source_end}:{case.source_text[case.source_start:case.source_end]}"
        ),
        "source_context_hash": _hash(_source_sentence(case)),
        "target_window_hash": _hash(window.text),
    }
    return _hash(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def validate_t2_payload(
    case: CascadeCase,
    window: TargetWindow,
    payload: Any,
    entry: RegistryEntry | None,
    *,
    llm_result: LLMResult | None = None,
) -> T2Decision:
    usage = llm_result.usage if llm_result is not None and not llm_result.from_cache else None
    base = {
        "occurrence_id": case.opaque_id,
        "from_cache": False,
        "api_cache_hit": bool(llm_result.from_cache) if llm_result else False,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "cost_usd": llm_result.cost_usd if llm_result is not None and not llm_result.from_cache else 0.0,
        "system_fingerprint": llm_result.system_fingerprint if llm_result else None,
    }
    if not isinstance(payload, dict) or str(payload.get("occurrence_id")) != case.opaque_id:
        return T2Decision(status="human_required", classification=None, quote="", start=None, end=None,
                          offset_source="none", reason="invalid_payload", **base)
    status = str(payload.get("status") or "")
    if status != "localized":
        if status not in {"omitted", "ambiguous", "not_found"}:
            status = "human_required"
        return T2Decision(status=status, classification=None, quote="", start=None, end=None,
                          offset_source="none", reason="model_abstained", **base)
    quote = str(payload.get("target_quote") or "")
    start = _int_or_none(payload.get("start"))
    end = _int_or_none(payload.get("end"))
    offset_source = "model"
    valid_model_offset = (
        start is not None and end is not None and 0 <= start < end <= len(window.text)
        and window.text[start:end] == quote
    )
    if not valid_model_offset:
        matches = [match.start() for match in re.finditer(re.escape(quote), window.text)] if quote else []
        if len(matches) == 1:
            start = matches[0]
            end = start + len(quote)
            offset_source = "unique_quote_reanchor"
        else:
            anchored = _position_reanchor(case, window, quote, matches)
            if anchored is None:
                return T2Decision(status="human_required", classification=None, quote=quote, start=None, end=None,
                                  offset_source="none", reason="offset_invalid_or_ambiguous", **base)
            start, end = anchored
            offset_source = "position_quote_reanchor"
    absolute_start = window.start + int(start)
    absolute_end = window.start + int(end)
    classification = classify_localized_quote(quote, entry)
    return T2Decision(
        status="localized", classification=classification, quote=quote,
        start=absolute_start, end=absolute_end, offset_source=offset_source,
        reason="validated", **base,
    )


def classify_localized_quote(quote: str, entry: RegistryEntry | None) -> str:
    if entry is None:
        return "novel"
    classes, conflicts = _form_classes(entry)
    key = _key(quote)
    if key in conflicts:
        return "registry_conflict"
    if key in classes:
        return classes[key][0]
    return "novel"


def localize_with_t2(
    case: CascadeCase,
    entry: RegistryEntry | None,
    *,
    client: LLMClient,
    result_cache: LocalizationResultCache,
    window_chars: int = DEFAULT_WINDOW_CHARS,
) -> T2Decision:
    window = target_window(case, max_chars=window_chars)
    key = result_cache_key(case, window, client.config)
    cached = result_cache.get(key)
    if cached is not None:
        return cached
    result = client.call(
        build_t2_messages(case, window), response_format=RESULT_SCHEMA,
        tag=f"localizer:{PROMPT_VERSION}:{case.opaque_id}",
    )
    decision = validate_t2_payload(case, window, result.parsed_json, entry, llm_result=result)
    result_cache.put(key, decision)
    return decision


def preflight_dev(
    *,
    gold_path: str | Path,
    db_path: str | Path,
    config: LLMConfig,
) -> tuple[dict[str, Any], list[CascadeCase], dict[str, RegistryEntry | None]]:
    rows, cases = load_dev_cases(gold_path, db_path)
    registry = load_registry(db_path)
    t1 = run_t1(cases, registry)
    legacy_ids = set(legacy_longest_failures(rows))
    pilot = [case for case in cases if case.row_id in legacy_ids]
    entries: dict[str, RegistryEntry | None] = {}
    prompts: list[dict[str, Any]] = []
    for case in pilot:
        entry, _ = resolve_registry_entry(registry, case.source_term, case.chapter_id)
        entries[case.opaque_id] = entry
        window = target_window(case)
        messages = build_t2_messages(case, window)
        prompts.append({
            "row_id": case.row_id,
            "opaque_id": case.opaque_id,
            "config_internal": case.config,
            "window": asdict(window),
            "messages": messages,
            "estimated_prompt_tokens": estimate_prompt_tokens(messages, RESULT_SCHEMA),
        })
    prompt_tokens = sum(item["estimated_prompt_tokens"] for item in prompts)
    max_output_tokens = len(pilot) * config.max_output_tokens
    worst_prompt = prompt_tokens * 3
    worst_output = max_output_tokens * 3
    report = {
        "mode": "preflight_dev",
        "dataset_role": "dev_regression_not_generalization",
        "gold_rows": len(rows),
        "legacy_longest_failure_rows": [case.row_id for case in pilot],
        "pilot_cases": len(pilot),
        "t1": {
            "resolved": sum(item.status == "resolved" for item in t1.values()),
            "residual": sum(item.status != "resolved" for item in t1.values()),
            "residual_reasons": dict(Counter(item.reason for item in t1.values() if item.status != "resolved")),
            "registry_entries_with_allowed_forbidden_overlap": registry_conflict_count(registry),
        },
        "model": asdict(config),
        "prompt_version": PROMPT_VERSION,
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_max_output_tokens": max_output_tokens,
        "estimated_total_tokens": prompt_tokens + max_output_tokens,
        "worst_case_three_stage_tokens": worst_prompt + worst_output,
        "estimated_cost_usd": _cost(config, prompt_tokens, max_output_tokens),
        "worst_case_cost_usd": _cost(config, worst_prompt, worst_output),
        "confirm_token": _confirm_token(config.model, PROMPT_VERSION, pilot, prompt_tokens, max_output_tokens),
        "prompts": prompts,
    }
    return report, pilot, entries


def score_dev_pilot(cases: list[CascadeCase], decisions: dict[str, T2Decision]) -> dict[str, Any]:
    by_config: dict[str, dict[str, Any]] = {}
    for config in ("S0", "S1"):
        scoped = [case for case in cases if case.config == config]
        localized = [decisions.get(case.opaque_id) for case in scoped]
        exact = sum(
            bool(decision and decision.start == case.gold_start and decision.end == case.gold_end)
            for case, decision in zip(scoped, localized)
        )
        region = sum(bool(decision and decision.status == "localized") for decision in localized)
        by_config[config] = {
            "n": len(scoped),
            "localized": region,
            "exact_span": exact,
            "exact_span_accuracy": exact / len(scoped) if scoped else None,
            "unresolved": len(scoped) - region,
        }
    total_exact = sum(item["exact_span"] for item in by_config.values())
    return {
        "dataset_role": "dev_regression_not_generalization",
        "per_config": by_config,
        "total": {
            "n": len(cases),
            "exact_span": total_exact,
            "exact_span_accuracy": total_exact / len(cases) if cases else None,
        },
        "note": "This is DEV localization accuracy, not D consistency and not held-out generalization.",
    }


def render_audit_html(cases: list[CascadeCase], decisions: dict[str, T2Decision]) -> str:
    rows = []
    for case in cases:
        decision = decisions.get(case.opaque_id)
        rows.append({
            "row_id": case.row_id,
            "source_term": case.source_term,
            "source_context": _mark_source_occurrence(_source_sentence(case), case.source_term),
            "target_text": case.target_text,
            "gold_quote": case.gold_quote,
            "gold_start": case.gold_start,
            "gold_end": case.gold_end,
            "decision": asdict(decision) if decision else None,
        })
    payload = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html lang=\"vi\"><meta charset=\"utf-8\"><title>EV-07b DEV pilot</title>
<style>body{{font:15px system-ui;margin:24px;max-width:1100px}}article{{border:1px solid #ccc;padding:14px;margin:12px 0}}pre{{white-space:pre-wrap}}.ok{{color:#087830}}.bad{{color:#b42318}}</style>
<h1>EV-07b DEV pilot — 8 legacy residuals</h1><p>DEV/regression only; not held-out generalization.</p><div id=\"app\"></div>
<script>const rows={payload};const esc=s=>String(s??'').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));
app.innerHTML=rows.map(r=>{{const d=r.decision||{{}};const ok=d.start===r.gold_start&&d.end===r.gold_end;return `<article><b>${{esc(r.row_id)}}</b> · <span class=\"${{ok?'ok':'bad'}}\">${{ok?'EXACT':'REVIEW'}}</span><p>Term: <code>${{esc(r.source_term)}}</code></p><pre>${{esc(r.source_context)}}</pre><p>Gold: <mark>${{esc(r.gold_quote)}}</mark></p><p>T2: <mark>${{esc(d.quote)}}</mark> [${{d.start}},${{d.end}}] · ${{esc(d.offset_source)}} · ${{esc(d.classification)}}</p><pre>${{esc(r.target_text)}}</pre></article>`}}).join('');</script></html>"""


def write_audit_csv(path: str | Path, cases: list[CascadeCase], decisions: dict[str, T2Decision]) -> None:
    fields = [
        "occurrence_id", "config", "source_term", "source_sentence", "target_window",
        "t2_status", "t2_quote", "t2_start", "t2_end", "human_verdict", "human_quote",
        "human_start", "human_end", "note",
    ]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for case in cases:
            decision = decisions.get(case.opaque_id)
            writer.writerow({
                "occurrence_id": case.opaque_id,
                "config": case.config,
                "source_term": case.source_term,
                "source_sentence": _source_sentence(case),
                "target_window": case.target_text,
                "t2_status": decision.status if decision else "",
                "t2_quote": decision.quote if decision else "",
                "t2_start": decision.start if decision and decision.start is not None else "",
                "t2_end": decision.end if decision and decision.end is not None else "",
            })


def _form_classes(entry: RegistryEntry) -> tuple[dict[str, tuple[str, str]], set[str]]:
    allowed = {_key(entry.target_term): entry.target_term}
    allowed.update({_key(value): value for value in entry.allowed if value})
    forbidden = {_key(value): value for value in entry.forbidden if value}
    conflicts = set(allowed) & set(forbidden)
    result: dict[str, tuple[str, str]] = {}
    for key, value in allowed.items():
        result[key] = ("registry_conflict" if key in conflicts else "known_allowed", value)
    for key, value in forbidden.items():
        result[key] = ("registry_conflict" if key in conflicts else "known_forbidden", value)
    return result, conflicts


def _is_short_known_form(matched: str, forms: dict[str, tuple[str, str]]) -> bool:
    if not matched:
        return False
    tokens = matched.split()
    for key in forms:
        longer = key.split()
        if len(longer) <= len(tokens):
            continue
        for index in range(len(longer) - len(tokens) + 1):
            if longer[index:index + len(tokens)] == tokens:
                return True
    return False


def _source_sentence(case: CascadeCase) -> str:
    left, right = _source_sentence_bounds(case)
    return case.source_text[left:right].strip()


def _marked_source_sentence(case: CascadeCase) -> str:
    left, right = _source_sentence_bounds(case)
    raw = case.source_text[left:right]
    relative_start = case.source_start - left
    relative_end = case.source_end - left
    marked = (
        raw[:relative_start]
        + "[[TERM_START]]"
        + raw[relative_start:relative_end]
        + "[[TERM_END]]"
        + raw[relative_end:]
    )
    return marked.strip()


def _mark_source_occurrence(context: str, term: str) -> str:
    match = re.search(re.escape(term), context, flags=re.IGNORECASE)
    if not match:
        return context
    return context[:match.start()] + "[[TERM_START]]" + context[match.start():match.end()] + "[[TERM_END]]" + context[match.end():]


def _source_sentence_bounds(case: CascadeCase) -> tuple[int, int]:
    left_candidates = [case.source_text.rfind(mark, 0, case.source_start) for mark in ".!?\n"]
    left = max(left_candidates) + 1
    right_values = [case.source_text.find(mark, case.source_end) for mark in ".!?\n"]
    right_candidates = [value for value in right_values if value >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(case.source_text)
    return left, right


def _position_reanchor(
    case: CascadeCase,
    window: TargetWindow,
    quote: str,
    starts: list[int],
) -> tuple[int, int] | None:
    if not quote or len(starts) < 2 or not case.source_text or not case.target_text:
        return None
    source_ratio = ((case.source_start + case.source_end) / 2) / len(case.source_text)
    expected_absolute = source_ratio * len(case.target_text)
    ranked = sorted(
        (
            abs((window.start + start + len(quote) / 2) - expected_absolute),
            start,
        )
        for start in starts
    )
    # Position is a hint, not proof. Accept only when the best occurrence is
    # separated from the runner-up by at least one quote width (minimum 8 chars).
    if ranked[1][0] - ranked[0][0] < max(8, len(quote)):
        return None
    start = ranked[0][1]
    return start, start + len(quote)


def _json_strings(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()] if isinstance(parsed, list) else []


def _key(value: Any) -> str:
    return re.sub(r"\s+", " ", normalize_surface(str(value or "")).casefold().strip())


def _opaque_id(row_id: str) -> str:
    return "occ_" + hashlib.sha256(row_id.encode("utf-8")).hexdigest()[:16]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cost(config: LLMConfig, input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens / 1_000_000 * config.pricing["input"]
        + output_tokens / 1_000_000 * config.pricing["output"],
        8,
    )


def _confirm_token(
    model: str, prompt_version: str, cases: list[CascadeCase], prompt_tokens: int, output_tokens: int
) -> str:
    payload = ":".join([
        model, prompt_version, ",".join(sorted(case.opaque_id for case in cases)),
        str(prompt_tokens), str(output_tokens),
    ])
    return "LOCALIZE-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12].upper()
