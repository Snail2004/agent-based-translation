from __future__ import annotations

import csv
import hashlib
import html
import json
import random
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig
from pipeline.eval.d2l_translate_score import (
    ScopeBlock,
    _load_translations,
    _resolve_chapters,
    _scope_blocks,
)
from pipeline.eval.surface_match import find_spans, normalize_surface
from pipeline.translate.profiles import get_profile


DEFAULT_EXPERIMENT_ID = "d2l_p3"
DEFAULT_PROFILE = "technical_d2l_v1"
DEFAULT_DOC_ID = "d2l"
DEFAULT_CHAPTERS = (
    "introduction",
    "preliminaries",
    "linear_networks",
    "multilayer_perceptrons",
)
DEFAULT_SEED = 42
DEFAULT_JUDGE_MODEL = "gemini-2.5-flash"
DEFAULT_AGREEMENT_TRUST_THRESHOLD = 0.80
JSON_FORMAT = {"type": "json_object"}
DUMMY_TERM = "widget coefficient"
DUMMY_A = "hệ số widget"
DUMMY_B = "widget coefficient"


@dataclass(frozen=True)
class OverrideItem:
    item_id: str
    source_term: str
    tier: str
    s0_surface: str
    s1_surface: str
    s0_count: int
    s1_count: int
    status_s0: str
    status_s1: str
    rep_block_id: str | None
    en_sentence: str
    s0_window: str
    s1_window: str
    rep_resolved: bool
    skip_reason: str = ""


@dataclass(frozen=True)
class WorksheetRow:
    item_id: str
    en_sentence: str
    en_term: str
    version_A: str
    version_B: str
    tier: str
    human_suggested: bool = False


@dataclass(frozen=True)
class KeyRow:
    item_id: str
    A: str
    B: str
    source_term: str
    tier: str
    s0_surface: str
    s1_surface: str
    rep_block_id: str


def build_override_set(
    report: dict[str, Any],
    db_path: str | Path,
    *,
    chapters: Iterable[str] = DEFAULT_CHAPTERS,
    experiment_id: str = DEFAULT_EXPERIMENT_ID,
    profile_name: str = DEFAULT_PROFILE,
    doc_id: str = DEFAULT_DOC_ID,
) -> list[OverrideItem]:
    """Build the post-hoc memory tradeoff set from frozen scorer output.

    Rule: keep terms where S0 is surface-consistent and S1's dominant surface
    differs from S0's dominant surface. This is a drift set, not a harm set.
    """

    terms_by_config = {
        config: _terms_by_source(report, config)
        for config in ("S0", "S1")
    }
    conn = _connect_readonly(db_path)
    conn.row_factory = sqlite3.Row
    try:
        profile = get_profile(profile_name)
        resolved_chapters = _resolve_chapters(conn, doc_id, list(chapters))
        blocks = _scope_blocks(conn, doc_id, resolved_chapters, profile)
        translations = {
            config: _load_translations(conn, experiment_id, config)
            for config in ("S0", "S1")
        }
    finally:
        conn.close()

    items: list[OverrideItem] = []
    for source_term in sorted(set(terms_by_config["S0"]) & set(terms_by_config["S1"])):
        s0 = terms_by_config["S0"][source_term]
        s1 = terms_by_config["S1"][source_term]
        if str(s0.get("status") or "") != "consistent":
            continue
        s0_surface, s0_count = dominant_surface(s0.get("forms_used") or {})
        s1_surface, s1_count = dominant_surface(s1.get("forms_used") or {})
        if not s0_surface or not s1_surface or _surface_key(s0_surface) == _surface_key(s1_surface):
            continue
        rep = _representative_context(
            blocks,
            translations,
            source_term=source_term,
            s0_surface=s0_surface,
            s1_surface=s1_surface,
        )
        item_id = stable_item_id(source_term)
        items.append(
            OverrideItem(
                item_id=item_id,
                source_term=source_term,
                tier=str(s0.get("constraint_strength") or s1.get("constraint_strength") or "unknown"),
                s0_surface=s0_surface,
                s1_surface=s1_surface,
                s0_count=s0_count,
                s1_count=s1_count,
                status_s0=str(s0.get("status") or ""),
                status_s1=str(s1.get("status") or ""),
                rep_block_id=rep["block_id"],
                en_sentence=rep["en_sentence"],
                s0_window=rep["s0_window"],
                s1_window=rep["s1_window"],
                rep_resolved=bool(rep["resolved"]),
                skip_reason=str(rep["skip_reason"]),
            )
        )
    return items


def build_judge_worksheet(
    items: list[OverrideItem],
    *,
    seed: int = DEFAULT_SEED,
) -> tuple[list[WorksheetRow], list[KeyRow]]:
    resolved = [item for item in items if item.rep_resolved]
    suggested = set(select_human_subset(resolved, seed=seed))
    rows: list[WorksheetRow] = []
    keys: list[KeyRow] = []
    for item in resolved:
        rng = random.Random(_seed_for_item(seed, item.item_id))
        systems = ["S0", "S1"]
        rng.shuffle(systems)
        windows = {"S0": item.s0_window, "S1": item.s1_window}
        a_system, b_system = systems
        rows.append(
            WorksheetRow(
                item_id=item.item_id,
                en_sentence=item.en_sentence,
                en_term=item.source_term,
                version_A=windows[a_system],
                version_B=windows[b_system],
                tier=item.tier,
                human_suggested=item.item_id in suggested,
            )
        )
        keys.append(
            KeyRow(
                item_id=item.item_id,
                A=a_system,
                B=b_system,
                source_term=item.source_term,
                tier=item.tier,
                s0_surface=item.s0_surface,
                s1_surface=item.s1_surface,
                rep_block_id=str(item.rep_block_id or ""),
            )
        )
    return rows, keys


def select_human_subset(
    items: list[OverrideItem],
    *,
    seed: int = DEFAULT_SEED,
    min_items: int = 12,
    max_items: int = 15,
) -> list[str]:
    if not items:
        return []
    by_tier: dict[str, list[OverrideItem]] = {}
    for item in items:
        by_tier.setdefault(item.tier, []).append(item)
    rng = random.Random(seed)
    for values in by_tier.values():
        rng.shuffle(values)

    selected: list[OverrideItem] = []
    for tier in sorted(by_tier):
        selected.append(by_tier[tier][0])

    pool = [item for values in by_tier.values() for item in values[1:]]
    rng.shuffle(pool)
    target = min(max_items, max(min_items, len(selected)))
    for item in pool:
        if len(selected) >= target:
            break
        selected.append(item)
    return [item.item_id for item in selected[:max_items]]


def render_worksheet_html(rows: list[WorksheetRow]) -> str:
    payload = json.dumps([asdict(row) for row in rows], ensure_ascii=False)
    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <title>Memory tradeoff blind worksheet</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 1100px; margin: 24px auto; line-height: 1.45; }}
    .item {{ border: 1px solid #d8dee4; border-radius: 8px; padding: 16px; margin: 14px 0; }}
    .suggested {{ border-left: 5px solid #0969da; }}
    .muted {{ color: #57606a; font-size: 13px; }}
    .version {{ padding: 10px; background: #f6f8fa; border-radius: 6px; margin: 8px 0; white-space: pre-wrap; }}
    mark {{ background: #fff3bf; padding: 0 2px; }}
    button {{ padding: 8px 12px; }}
  </style>
</head>
<body>
  <h1>Memory Tradeoff Blind Worksheet</h1>
  <p class="muted">Judge only the marked term rendering against the English source. Do not infer model identities.</p>
  <div id="items"></div>
  <button id="export">Export JSON</button>
  <script>
    const rows = {payload};
    const root = document.getElementById("items");
    function escapeHtml(value) {{
      return String(value).replace(/[&<>"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}})[ch]);
    }}
    function marked(value) {{
      return escapeHtml(value).replaceAll("«", "<mark>").replaceAll("»", "</mark>");
    }}
    for (const row of rows) {{
      const div = document.createElement("section");
      div.className = "item" + (row.human_suggested ? " suggested" : "");
      div.innerHTML = `
        <h2>${{escapeHtml(row.item_id)}} ${{row.human_suggested ? '<span class="muted">suggested subset</span>' : ''}}</h2>
        <p><strong>EN term:</strong> ${{escapeHtml(row.en_term)}}</p>
        <p><strong>EN source:</strong> ${{escapeHtml(row.en_sentence)}}</p>
        <p><strong>Version A</strong></p><div class="version">${{marked(row.version_A)}}</div>
        <p><strong>Version B</strong></p><div class="version">${{marked(row.version_B)}}</div>
        <label><input type="radio" name="${{row.item_id}}" value="A_better"> A better</label>
        <label><input type="radio" name="${{row.item_id}}" value="equivalent"> Equivalent</label>
        <label><input type="radio" name="${{row.item_id}}" value="B_better"> B better</label>
        <p><textarea data-note="${{row.item_id}}" rows="2" style="width:100%" placeholder="optional note"></textarea></p>
      `;
      root.appendChild(div);
    }}
    document.getElementById("export").addEventListener("click", () => {{
      const judgments = [];
      for (const row of rows) {{
        const checked = document.querySelector(`input[name="${{row.item_id}}"]:checked`);
        if (!checked) continue;
        const note = document.querySelector(`textarea[data-note="${{row.item_id}}"]`).value || "";
        judgments.push({{item_id: row.item_id, human_label: checked.value, note}});
      }}
      const blob = new Blob([JSON.stringify({{items: judgments}}, null, 2)], {{type: "application/json"}});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "judgments_human.json";
      a.click();
      URL.revokeObjectURL(url);
    }});
  </script>
</body>
</html>
"""


def render_judge_prompt(row: WorksheetRow, *, orientation: str = "forward") -> list[dict[str, str]]:
    if orientation not in {"forward", "reverse"}:
        raise ValueError("orientation must be forward or reverse")
    version_a = row.version_A if orientation == "forward" else row.version_B
    version_b = row.version_B if orientation == "forward" else row.version_A
    system = (
        "You are an independent English-to-Vietnamese terminology adequacy judge. "
        "You do not know which system produced either Vietnamese version. "
        "Judge only the marked term rendering between « and ». "
        "Return JSON only."
    )
    user = {
        "task": "Choose which Vietnamese marked rendering better conveys the English term in context.",
        "dummy_example": {
            "english_term": DUMMY_TERM,
            "version_A_marked": DUMMY_A,
            "version_B_marked": DUMMY_B,
            "note": "This is only a dummy example; do not use it to judge the real item.",
        },
        "english_source_sentence": row.en_sentence,
        "english_term": row.en_term,
        "version_A": version_a,
        "version_B": version_b,
        "rules": [
            "Judge adequacy against the English source and the marked English term.",
            "Consider natural Vietnamese technical register.",
            "Ignore unmarked differences unless they affect the marked term.",
            "Do not infer model identity or provenance.",
            "Use equivalent if both marked renderings are acceptable or the difference is immaterial.",
        ],
        "output_schema": {
            "label": "A_better|B_better|equivalent",
            "confidence": "number 0..1",
            "reason": "one short Vietnamese sentence",
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False, indent=2)},
    ]


def preview_judge_calls(
    rows: list[WorksheetRow],
    config: LLMConfig,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    model_name = model or config.model
    calls = len(rows) * 2
    prompt_tokens = 0
    for row in rows:
        prompt_tokens += estimate_prompt_tokens(render_judge_prompt(row, orientation="forward"), response_format=JSON_FORMAT)
        prompt_tokens += estimate_prompt_tokens(render_judge_prompt(row, orientation="reverse"), response_format=JSON_FORMAT)
    max_output_tokens = calls * config.max_output_tokens
    pricing = config.pricing
    cost = (
        (prompt_tokens / 1_000_000) * pricing["input"]
        + (max_output_tokens / 1_000_000) * pricing["output"]
    )
    return {
        "judge_model": model_name,
        "temperature": config.temperature,
        "seed": config.seed,
        "calls": calls,
        "items": len(rows),
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_max_output_tokens": max_output_tokens,
        "estimated_uncached_cost_usd": round(cost, 6),
        "confirm_token": confirm_token(model_name, calls, prompt_tokens, max_output_tokens),
        "caveats": [
            "judge inherits calibrated=false from EV-02 (no human-council calibration)",
            f"human-gemini trust threshold is fixed a priori at {DEFAULT_AGREEMENT_TRUST_THRESHOLD:.0%}",
            "model id is recorded exactly as called; Google stable ids may not expose a -NNN suffix",
        ],
    }


def confirm_token(model: str, calls: int, prompt_tokens: int, max_output_tokens: int) -> str:
    payload = f"memory_tradeoff:{model}:{calls}:{prompt_tokens}:{max_output_tokens}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def parse_judge_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        data = value
    elif isinstance(value, str):
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return _repair_judge_payload(value)
    else:
        data = {}
    label = normalize_label(data.get("label"))
    return {
        "label": label,
        "confidence": _float_or_none(data.get("confidence")),
        "reason": str(data.get("reason") or ""),
        "raw": data,
    }


def _repair_judge_payload(text: str) -> dict[str, Any]:
    """Recover label/confidence from a truncated judge JSON response.

    The recovery is intentionally narrow: it only trusts explicit JSON-looking
    fields already present in the text. It never invents a label.
    """

    label_match = re.search(r'"label"\s*:\s*"([^"]+)"', text)
    if label_match is None:
        raise ValueError("Judge JSON parse failed and no label field could be repaired")
    confidence_match = re.search(r'"confidence"\s*:\s*([0-9.]+)', text)
    reason_match = re.search(r'"reason"\s*:\s*"([^"]*)', text, flags=re.DOTALL)
    raw = {
        "_parse_repaired": True,
        "_raw_text_prefix": text[:500],
    }
    return {
        "label": normalize_label(label_match.group(1)),
        "confidence": _float_or_none(confidence_match.group(1)) if confidence_match else None,
        "reason": reason_match.group(1).replace("\n", " ").strip() if reason_match else "",
        "raw": raw,
    }


def resolve_pair(forward_label: str, reverse_label: str, key: KeyRow) -> str:
    first = display_label_to_system(forward_label, key, orientation="forward")
    second = display_label_to_system(reverse_label, key, orientation="reverse")
    if first == second and first in {"S0_better", "S1_better", "equivalent"}:
        return first
    return "ambiguous"


def system_label_to_final(label: str) -> str:
    if label == "S1_better":
        return "improve"
    if label == "S0_better":
        return "harm"
    return "lateral"


def display_label_to_system(label: str, key: KeyRow, *, orientation: str = "forward") -> str:
    normalized = normalize_label(label)
    if normalized == "equivalent":
        return "equivalent"
    if orientation == "forward":
        a_system, b_system = key.A, key.B
    elif orientation == "reverse":
        a_system, b_system = key.B, key.A
    else:
        raise ValueError("orientation must be forward or reverse")
    if normalized == "A_better":
        return f"{a_system}_better"
    if normalized == "B_better":
        return f"{b_system}_better"
    return "ambiguous"


def normalize_label(value: Any) -> str:
    text = str(value or "").strip()
    mapping = {
        "a": "A_better",
        "a_better": "A_better",
        "A_better": "A_better",
        "b": "B_better",
        "b_better": "B_better",
        "B_better": "B_better",
        "tie": "equivalent",
        "equivalent": "equivalent",
        "same": "equivalent",
        "lateral": "equivalent",
    }
    return mapping.get(text, "equivalent")


def score_memory_tradeoff(
    *,
    worksheet_rows: list[WorksheetRow],
    key_rows: list[KeyRow],
    gemini_rows: list[dict[str, Any]],
    human_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    key_by_id = {row.item_id: row for row in key_rows}
    worksheet_by_id = {row.item_id: row for row in worksheet_rows}
    gemini_by_item: dict[str, dict[str, dict[str, Any]]] = {}
    for row in gemini_rows:
        item_id = str(row.get("item_id") or "")
        orientation = str(row.get("orientation") or "")
        if item_id and orientation:
            gemini_by_item.setdefault(item_id, {})[orientation] = row
    missing = [
        item_id
        for item_id in key_by_id
        if "forward" not in gemini_by_item.get(item_id, {}) or "reverse" not in gemini_by_item.get(item_id, {})
    ]
    if missing:
        raise ValueError(f"blind not complete: missing Gemini judgments for {len(missing)} items")

    human_by_id = {str(row.get("item_id") or ""): row for row in human_rows if row.get("item_id")}

    items: list[dict[str, Any]] = []
    agreement_pairs: list[tuple[str, str]] = []
    for item_id, key in sorted(key_by_id.items()):
        forward = parse_judge_payload(gemini_by_item[item_id]["forward"].get("judgment") or gemini_by_item[item_id]["forward"])
        reverse = parse_judge_payload(gemini_by_item[item_id]["reverse"].get("judgment") or gemini_by_item[item_id]["reverse"])
        gemini_resolved = resolve_pair(forward["label"], reverse["label"], key)
        gemini_final = system_label_to_final(gemini_resolved)
        human_system = None
        human_final = None
        human = human_by_id.get(item_id)
        if human:
            human_system = display_label_to_system(str(human.get("human_label") or ""), key, orientation="forward")
            human_final = system_label_to_final(human_system)
            agreement_pairs.append((human_system, gemini_resolved))
        final_label = human_final or gemini_final
        worksheet = worksheet_by_id[item_id]
        items.append(
            {
                "item_id": item_id,
                "term": key.source_term,
                "tier": key.tier,
                "rep_block_id": key.rep_block_id,
                "consistency_status": "S0_consistent_to_S1_changed_form",
                "s0_surface": key.s0_surface,
                "s1_surface": key.s1_surface,
                "gemini_forward": forward,
                "gemini_reverse": reverse,
                "gemini_resolved": gemini_resolved,
                "gemini_label": gemini_final,
                "human_label": human.get("human_label") if human else None,
                "human_system_label": human_system,
                "human_final_label": human_final,
                "final_label": final_label,
                "human_suggested": worksheet.human_suggested,
            }
        )

    agreement = _agreement(agreement_pairs)
    return {
        "metric_version": "memory_tradeoff_judge_v1",
        "n": len(items),
        "summary": _summary(items, key="final_label"),
        "summary_gemini_only": _summary(items, key="gemini_label"),
        "by_tier": _by_tier(items, key="final_label"),
        "iaa": {
            "human_n": len(agreement_pairs),
            "agreement_human_vs_gemini": agreement,
            "trust_threshold": DEFAULT_AGREEMENT_TRUST_THRESHOLD,
            "trust_gemini_for_full_set": bool(agreement is not None and agreement >= DEFAULT_AGREEMENT_TRUST_THRESHOLD),
            "kappa": None,
            "single_annotator": True,
        },
        "items": items,
        "caveats": [
            "N=57/59-style override diagnostic; source-of-truth is rule-based drift set, not a fixed count",
            "solo annotator -> no kappa -> occurrence-level diagnostic_only",
            "judge inherits calibrated=false from EV-02 (no human-council calibration)",
            f"agreement threshold fixed a priori at {DEFAULT_AGREEMENT_TRUST_THRESHOLD:.0%}: below threshold means human should label all items",
            "0-API consistency drift is not harm; improve/lateral/harm requires judge labels",
            "post-hoc eval-only: frozen translation outputs are not mutated",
        ],
    }


def write_override_csv(path: str | Path, items: list[OverrideItem]) -> None:
    _write_csv(path, [asdict(item) for item in items])


def write_worksheet_jsonl(path: str | Path, rows: list[WorksheetRow]) -> None:
    write_jsonl(path, [asdict(row) for row in rows])


def write_key_json(path: str | Path, key_rows: list[KeyRow], *, seed: int) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seed": seed,
        "blind_warning": "DO NOT OPEN until judgments_human.json and judgments_gemini.jsonl are complete.",
        "items": [asdict(row) for row in key_rows],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_worksheet_jsonl(path: str | Path) -> list[WorksheetRow]:
    return [WorksheetRow(**row) for row in read_jsonl(path)]


def read_key_json(path: str | Path) -> list[KeyRow]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [KeyRow(**row) for row in payload.get("items", [])]


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if text:
                rows.append(json.loads(text))
    return rows


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def read_human_json(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("items", [])
    else:
        rows = payload
    return [dict(row) for row in rows if isinstance(row, dict)]


def write_gemini_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    write_jsonl(path, rows)


def stable_item_id(source_term: str) -> str:
    digest = hashlib.sha1(_surface_key(source_term).encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "_", _surface_key(source_term)).strip("_")[:40] or "term"
    return f"mt_{slug}_{digest}"


def dominant_surface(forms: dict[str, Any]) -> tuple[str, int]:
    if not forms:
        return "", 0
    surface, count = sorted(
        ((str(surface), int(count)) for surface, count in forms.items()),
        key=lambda item: (-item[1], _surface_key(item[0])),
    )[0]
    return surface, count


def validate_dummy_not_real(
    rows: list[WorksheetRow],
    dummy_terms: Iterable[str] = (DUMMY_TERM, DUMMY_A, DUMMY_B),
) -> None:
    parts: list[str] = []
    for row in rows:
        parts.extend([row.en_term, row.en_sentence, row.version_A, row.version_B])
    haystack = "\n".join(parts)
    normalized = _surface_key(haystack)
    for term in dummy_terms:
        if _surface_key(term) in normalized:
            raise ValueError(f"Dummy prompt example leaks a real worksheet surface: {term}")


def _terms_by_source(report: dict[str, Any], config: str) -> dict[str, dict[str, Any]]:
    try:
        terms = report["D_registry_consistency"][config]["terms_all"]
    except KeyError as exc:
        raise ValueError(f"Report missing D_registry_consistency.{config}.terms_all") from exc
    return {
        str(row.get("source_term") or ""): dict(row)
        for row in terms
        if str(row.get("source_term") or "").strip()
    }


def _connect_readonly(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).resolve().as_posix()
    return sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)


def _representative_context(
    blocks: list[ScopeBlock],
    translations: dict[str, dict[str, str]],
    *,
    source_term: str,
    s0_surface: str,
    s1_surface: str,
) -> dict[str, Any]:
    fallback_source = ""
    for block in blocks:
        source_spans = find_spans(block.text, source_term, language="en")
        if not source_spans:
            continue
        fallback_source = fallback_source or _sentence_for_span(block.text, source_spans[0][0], source_spans[0][1])
        s0_text = translations["S0"].get(block.block_id, "")
        s1_text = translations["S1"].get(block.block_id, "")
        s0_spans = find_spans(s0_text, s0_surface, language="vi")
        s1_spans = find_spans(s1_text, s1_surface, language="vi")
        if not s0_spans or not s1_spans:
            continue
        return {
            "resolved": True,
            "skip_reason": "",
            "block_id": block.block_id,
            "en_sentence": _sentence_for_span(block.text, source_spans[0][0], source_spans[0][1]),
            "s0_window": _mark_sentence_for_span(s0_text, s0_spans[0][0], s0_spans[0][1]),
            "s1_window": _mark_sentence_for_span(s1_text, s1_spans[0][0], s1_spans[0][1]),
        }
    return {
        "resolved": False,
        "skip_reason": "no block contains source term plus both dominant target surfaces",
        "block_id": None,
        "en_sentence": fallback_source,
        "s0_window": "",
        "s1_window": "",
    }


def _sentence_for_span(text: str, start: int, end: int) -> str:
    value = str(text or "")
    left = max(value.rfind(".", 0, start), value.rfind("?", 0, start), value.rfind("!", 0, start), value.rfind("\n", 0, start))
    right_candidates = [idx for idx in [value.find(".", end), value.find("?", end), value.find("!", end), value.find("\n", end)] if idx >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(value)
    return re.sub(r"\s+", " ", value[left + 1:right]).strip()


def _mark_sentence_for_span(text: str, start: int, end: int) -> str:
    sentence = _sentence_for_span(text, start, end)
    value = str(text or "")
    sentence_start = value.find(sentence)
    if sentence_start < 0 or not sentence:
        return value[:start] + "«" + value[start:end] + "»" + value[end:]
    local_start = max(0, start - sentence_start)
    local_end = max(local_start, end - sentence_start)
    return sentence[:local_start] + "«" + sentence[local_start:local_end] + "»" + sentence[local_end:]


def _summary(items: list[dict[str, Any]], *, key: str) -> dict[str, Any]:
    counts = Counter(str(item.get(key) or "lateral") for item in items)
    total = len(items)
    return {
        "n": total,
        "counts": dict(counts),
        "improve_pct": _ratio(counts["improve"], total),
        "lateral_pct": _ratio(counts["lateral"], total),
        "harm_pct": _ratio(counts["harm"], total),
    }


def _by_tier(items: list[dict[str, Any]], *, key: str) -> dict[str, Any]:
    tiers = sorted({str(item.get("tier") or "unknown") for item in items})
    return {
        tier: _summary([item for item in items if str(item.get("tier") or "unknown") == tier], key=key)
        for tier in tiers
    }


def _agreement(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    return sum(1 for human, gemini in pairs if human == gemini) / len(pairs)


def _ratio(count: int, total: int) -> float:
    return count / total if total else 0.0


def _write_csv(path: str | Path, rows: list[dict[str, Any]]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        out.write_text("", encoding="utf-8")
        return
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _seed_for_item(seed: int, item_id: str) -> int:
    digest = hashlib.sha256(f"{seed}:{item_id}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _surface_key(value: Any) -> str:
    return normalize_surface(str(value or "")).casefold().strip()


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
