"""Agreement analysis for the MLP evaluation artifacts.

This script is intentionally offline-only: it reads official report artifacts
already present on disk and writes a machine-readable JSON plus a Markdown
summary. It must not call any model/API endpoint.
"""

from __future__ import annotations

import csv
import json
import math
import re
import subprocess
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "data" / "reports" / "exp_s0s1_builderv2_v1"
CHAPTER_ID = "d2l_multilayer_perceptrons"
EXPERIMENT_ID = "exp_s0s1_builderv2_v1"
CONFIGS = ("S0", "S1")
SCALES = (
    "TC-Occ",
    "TA-Occ",
    "SF-QE",
    "SF-BT-cos",
    "SF-BT-llm",
    "PJ",
)


ARTIFACTS = {
    "metrics": REPORT_DIR / "metrics_mlp.json",
    "metrics_occ_csv": REPORT_DIR / "metrics_mlp_occurrence_audit.csv",
    "cascade_S0": REPORT_DIR / "cascade_mlp_S0.json",
    "cascade_S1": REPORT_DIR / "cascade_mlp_S1.json",
    "sf_qe": REPORT_DIR / "sf_qe_cometkiwi_wmt22.json",
    "sf_bt": REPORT_DIR / "sf_bt_mlp_full_stage1.json",
    "pj": REPORT_DIR / "pj_full_339.json",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("/", "\\")


def json_pointer(*parts: str) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "").casefold().strip())


def snippet(value: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unavailable"


def bottom_percentile_flags(
    scores: dict[str, float],
    *,
    artifact: Path,
    value_pointer_prefix: str,
    percentile: float = 0.10,
) -> tuple[set[str], dict[str, Any], dict[str, dict[str, Any]]]:
    """Return bottom percentile block ids, threshold metadata, and per-block evidence."""
    n = len(scores)
    k = max(1, math.ceil(n * percentile))
    ordered = sorted(scores.items(), key=lambda item: (item[1], item[0]))
    selected = ordered[:k]
    threshold = selected[-1][1] if selected else None
    flags = {block_id for block_id, _ in selected}
    evidence = {
        block_id: {
            "score": score,
            "artifact_path": rel(artifact),
            "artifact_pointer": f"{value_pointer_prefix}/{block_id}",
            "threshold_bottom_k": k,
            "threshold_score": threshold,
        }
        for block_id, score in selected
    }
    meta = {
        "n": n,
        "bottom_percent": percentile,
        "bottom_k": k,
        "threshold_score": threshold,
        "artifact_path": rel(artifact),
    }
    return flags, meta, evidence


@dataclass
class CascadeOcc:
    block_id: str
    term_id: str
    source_term: str
    rendered: str
    accepted: bool
    accepted_forms: list[str]
    decision: dict[str, Any]


def rendered_surface(decision: dict[str, Any]) -> str:
    if decision.get("target_surface"):
        return str(decision["target_surface"])
    score = decision.get("t3_code_score") or {}
    if score.get("target_quote_clean"):
        return str(score["target_quote_clean"])
    if decision.get("target_quote"):
        return str(decision["target_quote"])
    return ""


def is_ta_accepted(decision: dict[str, Any]) -> bool:
    if decision.get("resolved_by") == "t2_credit" and decision.get("decision") == "rendered":
        return True
    label = ((decision.get("t3_code_score") or {}).get("adherence_label") or "").strip()
    return label == "adherent"


def load_cascade_occurrences(config: str) -> list[CascadeOcc]:
    data = load_json(ARTIFACTS[f"cascade_{config}"])
    out: list[CascadeOcc] = []
    for decision in data["decisions"]:
        accepted = is_ta_accepted(decision)
        rendered = rendered_surface(decision)
        out.append(
            CascadeOcc(
                block_id=decision["block_id"],
                term_id=decision["term_id"],
                source_term=decision.get("source_term") or "",
                rendered=rendered,
                accepted=accepted,
                accepted_forms=list(decision.get("accepted_forms") or []),
                decision=decision,
            )
        )
    return out


def compute_tc_ta_occ() -> tuple[dict[str, Any], dict[str, dict[str, set[str]]], dict[str, dict[str, dict[str, Any]]]]:
    summaries: dict[str, Any] = {}
    flags: dict[str, dict[str, set[str]]] = {cfg: {"TC-Occ": set(), "TA-Occ": set()} for cfg in CONFIGS}
    evidence: dict[str, dict[str, dict[str, Any]]] = {cfg: {"TC-Occ": {}, "TA-Occ": {}} for cfg in CONFIGS}

    for cfg in CONFIGS:
        occs = load_cascade_occurrences(cfg)
        # TA-Occ: every off-glossary/not-rendered localized occurrence is a violation.
        ta_good = 0
        ta_bad = 0
        for occ in occs:
            if occ.accepted:
                ta_good += 1
            else:
                ta_bad += 1
                flags[cfg]["TA-Occ"].add(occ.block_id)
                entry = evidence[cfg]["TA-Occ"].setdefault(
                    occ.block_id,
                    {
                        "artifact_path": rel(ARTIFACTS[f"cascade_{cfg}"]),
                        "violation_count": 0,
                        "examples": [],
                    },
                )
                entry["violation_count"] += 1
                if len(entry["examples"]) < 5:
                    entry["examples"].append(
                        {
                            "source_term": occ.source_term,
                            "rendered": occ.rendered,
                            "accepted_forms": occ.accepted_forms[:6],
                            "resolved_by": occ.decision.get("resolved_by"),
                            "decision": occ.decision.get("decision"),
                            "adherence_label": (occ.decision.get("t3_code_score") or {}).get("adherence_label"),
                        }
                    )

        # TC-Occ: not_rendered occurrences are excluded, then non-majority renderings are violations.
        tc_rows: list[CascadeOcc] = []
        by_term: dict[str, Counter[str]] = defaultdict(Counter)
        for occ in occs:
            if occ.decision.get("decision") == "not_rendered":
                continue
            rendered = norm_text(occ.rendered)
            if not rendered:
                continue
            tc_rows.append(occ)
            by_term[occ.term_id][rendered] += 1

        majority_by_term = {
            term_id: counter.most_common(1)[0][0]
            for term_id, counter in by_term.items()
            if counter
        }
        tc_good = 0
        tc_bad = 0
        for occ in tc_rows:
            rendered = norm_text(occ.rendered)
            majority = majority_by_term.get(occ.term_id)
            if rendered == majority:
                tc_good += 1
            else:
                tc_bad += 1
                flags[cfg]["TC-Occ"].add(occ.block_id)
                entry = evidence[cfg]["TC-Occ"].setdefault(
                    occ.block_id,
                    {
                        "artifact_path": rel(ARTIFACTS[f"cascade_{cfg}"]),
                        "violation_count": 0,
                        "examples": [],
                    },
                )
                entry["violation_count"] += 1
                if len(entry["examples"]) < 5:
                    entry["examples"].append(
                        {
                            "source_term": occ.source_term,
                            "rendered": occ.rendered,
                            "majority_rendering": majority,
                            "term_id": occ.term_id,
                        }
                    )

        summaries[cfg] = {
            "TC-Occ": {
                "numerator_majority": tc_good,
                "denominator_localized": tc_good + tc_bad,
                "score": tc_good / (tc_good + tc_bad) if (tc_good + tc_bad) else None,
                "flag_blocks": len(flags[cfg]["TC-Occ"]),
                "artifact_path": rel(ARTIFACTS[f"cascade_{cfg}"]),
                "method_note": "Computed from cascade decisions: T2 target_surface / T3 target_quote_clean, not_rendered excluded.",
            },
            "TA-Occ": {
                "numerator_accepted": ta_good,
                "denominator_all_occurrences": ta_good + ta_bad,
                "score": ta_good / (ta_good + ta_bad) if (ta_good + ta_bad) else None,
                "flag_blocks": len(flags[cfg]["TA-Occ"]),
                "artifact_path": rel(ARTIFACTS[f"cascade_{cfg}"]),
                "method_note": "Computed from cascade decisions with accepted_forms in the MLP artifact. No separate MLP fragment-filter log was found on disk.",
            },
        }

    return summaries, flags, evidence


def load_block_texts(sf_bt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    blocks: dict[str, dict[str, Any]] = {}
    for item in sf_bt["items"]:
        block = blocks.setdefault(
            item["block_id"],
            {
                "block_id": item["block_id"],
                "chapter_id": item["chapter_id"],
                "order_index": item["order_index"],
                "block_type": item["block_type"],
                "source_text": item["source_text"],
                "outputs": {},
            },
        )
        block["outputs"][item["config"]] = item["output_text"]
    return blocks


def build_sf_qe_flags(sf_qe: dict[str, Any]) -> tuple[dict[str, set[str]], dict[str, Any], dict[str, dict[str, Any]]]:
    flags: dict[str, set[str]] = {}
    meta: dict[str, Any] = {}
    evidence: dict[str, dict[str, Any]] = {}
    pairs = [p for p in sf_qe["paired_delta"]["pairs"] if p["chapter_id"] == CHAPTER_ID]
    for cfg in CONFIGS:
        score_key = f"{cfg}_score"
        scores = {p["block_id"]: float(p[score_key]) for p in pairs}
        f, m, ev = bottom_percentile_flags(
            scores,
            artifact=ARTIFACTS["sf_qe"],
            value_pointer_prefix=f"/paired_delta/pairs/*/{score_key}",
        )
        flags[cfg] = f
        meta[cfg] = m
        evidence[cfg] = ev
    return flags, meta, evidence


def build_sf_bt_flags(sf_bt: dict[str, Any]) -> tuple[dict[str, dict[str, set[str]]], dict[str, Any], dict[str, dict[str, dict[str, Any]]]]:
    scores: dict[str, dict[str, dict[str, float]]] = {
        cfg: {"SF-BT-cos": {}, "SF-BT-llm": {}} for cfg in CONFIGS
    }
    for item in sf_bt["items"]:
        if item["chapter_id"] != CHAPTER_ID:
            continue
        cfg = item["config"]
        scores[cfg]["SF-BT-cos"][item["block_id"]] = float(item["bt_bge_cosine"]["score"])
        scores[cfg]["SF-BT-llm"][item["block_id"]] = float(item["bt_llm_score"]["score_mean"])

    flags: dict[str, dict[str, set[str]]] = {cfg: {} for cfg in CONFIGS}
    meta: dict[str, Any] = {cfg: {} for cfg in CONFIGS}
    evidence: dict[str, dict[str, dict[str, Any]]] = {cfg: {} for cfg in CONFIGS}
    for cfg in CONFIGS:
        for scale in ("SF-BT-cos", "SF-BT-llm"):
            f, m, ev = bottom_percentile_flags(
                scores[cfg][scale],
                artifact=ARTIFACTS["sf_bt"],
                value_pointer_prefix=f"/items/*/{scale}",
            )
            flags[cfg][scale] = f
            meta[cfg][scale] = m
            evidence[cfg][scale] = ev
    return flags, meta, evidence


def build_pj_flags(pj: dict[str, Any]) -> tuple[dict[str, set[str]], dict[str, Any], dict[str, dict[str, Any]]]:
    flags: dict[str, set[str]] = {cfg: set() for cfg in CONFIGS}
    evidence: dict[str, dict[str, Any]] = {cfg: {} for cfg in CONFIGS}
    all_cases = list(pj["cases"]) + list(pj.get("auto_tie_cases") or [])
    for case in all_cases:
        final = case.get("final") or {}
        verdict = final.get("overall_final")
        if verdict == "TIE" or case.get("category") == "AUTO-TIE":
            continue
        winner_arm = None
        if verdict == "A":
            winner_arm = case.get("candidate_a_arm")
        elif verdict == "B":
            winner_arm = case.get("candidate_b_arm")
        if winner_arm not in CONFIGS:
            continue
        loser = "S1" if winner_arm == "S0" else "S0"
        block_id = case["block_id"]
        flags[loser].add(block_id)
        evidence[loser][block_id] = {
            "artifact_path": rel(ARTIFACTS["pj"]),
            "case_id": case["id"],
            "winner_arm": winner_arm,
            "overall_final": verdict,
            "style_final": final.get("style_final"),
            "tags_final": final.get("tags_final") or [],
            "short_block": case.get("short_block"),
        }
    meta = {
        "artifact_path": rel(ARTIFACTS["pj"]),
        "definition": "PJ flag = decisive overall loss for the arm; auto ties and TIE verdicts are not flags.",
    }
    return flags, meta, evidence


def d1_summary(metrics: dict[str, Any], sf_qe: dict[str, Any], sf_bt: dict[str, Any], pj: dict[str, Any], tc_ta_occ: dict[str, Any]) -> list[dict[str, Any]]:
    d1: list[dict[str, Any]] = []
    d1.append(
        {
            "scale": "TC",
            "axis": "term consistency",
            "reference_frame": "tu-nhat-quan",
            "S0": metrics["D_registry_consistency"]["S0"]["overall"],
            "S1": metrics["D_registry_consistency"]["S1"]["overall"],
            "delta_S1_minus_S0": metrics["D_registry_consistency"]["S1"]["overall"] - metrics["D_registry_consistency"]["S0"]["overall"],
            "conclusion": "S1 improves block-level registry consistency.",
            "sources": {
                "S0": {"artifact_path": rel(ARTIFACTS["metrics"]), "pointer": json_pointer("D_registry_consistency", "S0", "overall"), "value_read": metrics["D_registry_consistency"]["S0"]["overall"]},
                "S1": {"artifact_path": rel(ARTIFACTS["metrics"]), "pointer": json_pointer("D_registry_consistency", "S1", "overall"), "value_read": metrics["D_registry_consistency"]["S1"]["overall"]},
            },
        }
    )
    d1.append(
        {
            "scale": "TC-Occ",
            "axis": "term consistency per occurrence",
            "reference_frame": "tu-nhat-quan",
            "S0": tc_ta_occ["S0"]["TC-Occ"]["score"],
            "S1": tc_ta_occ["S1"]["TC-Occ"]["score"],
            "delta_S1_minus_S0": tc_ta_occ["S1"]["TC-Occ"]["score"] - tc_ta_occ["S0"]["TC-Occ"]["score"],
            "conclusion": "S1 improves per-occurrence rendering consistency.",
            "sources": {
                cfg: {"artifact_path": tc_ta_occ[cfg]["TC-Occ"]["artifact_path"], "value_read": tc_ta_occ[cfg]["TC-Occ"]}
                for cfg in CONFIGS
            },
        }
    )
    d1.append(
        {
            "scale": "TA",
            "axis": "external gold adherence",
            "reference_frame": "gold-convention",
            "S0": metrics["B_gold_occurrence_adherence"]["S0"]["flat"]["adherence_lower"],
            "S1": metrics["B_gold_occurrence_adherence"]["S1"]["flat"]["adherence_lower"],
            "delta_S1_minus_S0": metrics["B_gold_occurrence_adherence"]["S1"]["flat"]["adherence_lower"] - metrics["B_gold_occurrence_adherence"]["S0"]["flat"]["adherence_lower"],
            "conclusion": "S1 improves block-level occurrence adherence to the gold ruler.",
            "sources": {
                "S0": {"artifact_path": rel(ARTIFACTS["metrics"]), "pointer": json_pointer("B_gold_occurrence_adherence", "S0", "flat", "adherence_lower"), "value_read": metrics["B_gold_occurrence_adherence"]["S0"]["flat"]["adherence_lower"]},
                "S1": {"artifact_path": rel(ARTIFACTS["metrics"]), "pointer": json_pointer("B_gold_occurrence_adherence", "S1", "flat", "adherence_lower"), "value_read": metrics["B_gold_occurrence_adherence"]["S1"]["flat"]["adherence_lower"]},
            },
        }
    )
    d1.append(
        {
            "scale": "TA-Occ",
            "axis": "approved-form adherence per occurrence",
            "reference_frame": "gold+notebook approved forms",
            "S0": tc_ta_occ["S0"]["TA-Occ"]["score"],
            "S1": tc_ta_occ["S1"]["TA-Occ"]["score"],
            "delta_S1_minus_S0": tc_ta_occ["S1"]["TA-Occ"]["score"] - tc_ta_occ["S0"]["TA-Occ"]["score"],
            "conclusion": "S1 improves per-occurrence approved-form landing.",
            "proxy_warning": (
                "PROXY - NOT the official gold-ruler TA-Occ of EVAL SS8c (official = preliminaries). "
                "Ruler here = cascade accepted_forms (notebook canonical + variants), which INCLUDES the "
                "known-bad canonical 'chuan hoa' for regularization -> partially self-referential and "
                "lenient toward S1. Use only as 'tuan thu tu dien tu xay' (production-mode metric), "
                "never present as TA-vs-gold."
            ),
            "sources": {
                cfg: {"artifact_path": tc_ta_occ[cfg]["TA-Occ"]["artifact_path"], "value_read": tc_ta_occ[cfg]["TA-Occ"]}
                for cfg in CONFIGS
            },
        }
    )
    qe_s0 = sf_qe["per_chapter_arm"][CHAPTER_ID]["S0"]["mean"]
    qe_s1 = sf_qe["per_chapter_arm"][CHAPTER_ID]["S1"]["mean"]
    d1.append(
        {
            "scale": "SF-QE",
            "axis": "reference-free quality estimation",
            "reference_frame": "bao-toan-nghia",
            "S0": qe_s0,
            "S1": qe_s1,
            "delta_S1_minus_S0": qe_s1 - qe_s0,
            "conclusion": "SF-QE is essentially neutral with a tiny S1 advantage.",
            "sources": {
                "S0": {"artifact_path": rel(ARTIFACTS["sf_qe"]), "pointer": json_pointer("per_chapter_arm", CHAPTER_ID, "S0", "mean"), "value_read": qe_s0},
                "S1": {"artifact_path": rel(ARTIFACTS["sf_qe"]), "pointer": json_pointer("per_chapter_arm", CHAPTER_ID, "S1", "mean"), "value_read": qe_s1},
            },
        }
    )
    bt_row: dict[str, Any] = {
        "scale": "SF-BT",
        "axis": "back-translation semantic similarity",
        "reference_frame": "bao-toan-nghia",
        "subscores": {},
        "conclusion": "SF-BT is mostly neutral; the LLM branch penalizes S1 on the known regularization/chuẩn hóa cluster.",
    }
    for metric_name in ("SF-BT-cos", "SF-BT-llm"):
        s0 = sf_bt["aggregates"]["all"]["per_chapter_arm"][metric_name][CHAPTER_ID]["S0"]["mean"]
        s1 = sf_bt["aggregates"]["all"]["per_chapter_arm"][metric_name][CHAPTER_ID]["S1"]["mean"]
        bt_row["subscores"][metric_name] = {
            "S0": s0,
            "S1": s1,
            "delta_S1_minus_S0": s1 - s0,
            "sources": {
                "S0": {"artifact_path": rel(ARTIFACTS["sf_bt"]), "pointer": json_pointer("aggregates", "all", "per_chapter_arm", metric_name, CHAPTER_ID, "S0", "mean"), "value_read": s0},
                "S1": {"artifact_path": rel(ARTIFACTS["sf_bt"]), "pointer": json_pointer("aggregates", "all", "per_chapter_arm", metric_name, CHAPTER_ID, "S1", "mean"), "value_read": s1},
            },
        }
    d1.append(bt_row)
    pj_agg = pj["aggregates"]["full_plus_all_auto_ties"]
    s0_wins = pj_agg["overall_win_loss_by_arm"].get("S0", 0)
    s1_wins = pj_agg["overall_win_loss_by_arm"].get("S1", 0)
    ties = pj_agg["overall_win_loss_by_arm"].get("TIE", 0)
    d1.append(
        {
            "scale": "PJ",
            "axis": "paired reader preference",
            "reference_frame": "taste-pho-thong",
            "S0": s0_wins,
            "S1": s1_wins,
            "delta_S1_minus_S0": s1_wins - s0_wins,
            "tie": ties,
            "conclusion": "PJ decisive wins favor S0, while most pairs are ties.",
            "sources": {
                "overall_win_loss_by_arm": {"artifact_path": rel(ARTIFACTS["pj"]), "pointer": json_pointer("aggregates", "full_plus_all_auto_ties", "overall_win_loss_by_arm"), "value_read": pj_agg["overall_win_loss_by_arm"]},
            },
        }
    )
    return d1


def load_metric_occurrence_gold_csv() -> dict[str, Any]:
    counts: dict[str, Counter[str]] = {cfg: Counter() for cfg in CONFIGS}
    examples: dict[str, dict[str, list[dict[str, str]]]] = {cfg: defaultdict(list) for cfg in CONFIGS}
    with ARTIFACTS["metrics_occ_csv"].open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row["ruler"] == "gold" and row["side"] == "target" and row["role"] == "active" and row["credited"] == "false":
                cfg = row["config"]
                if cfg in CONFIGS:
                    counts[cfg][row["block_id"]] += 1
                    if len(examples[cfg][row["block_id"]]) < 3:
                        examples[cfg][row["block_id"]].append(
                            {
                                "term_ids": row["term_ids"],
                                "form": row["form"],
                                "surface": row["surface"],
                                "context": row["context"],
                            }
                        )
    return {"counts": counts, "examples": examples, "artifact_path": rel(ARTIFACTS["metrics_occ_csv"])}


def pairwise_overlap(flags: dict[str, set[str]], n: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, a in enumerate(SCALES):
        for b in SCALES[i + 1 :]:
            set_a = flags.get(a, set())
            set_b = flags.get(b, set())
            inter = set_a & set_b
            union = set_a | set_b
            expected = (len(set_a) * len(set_b) / n) if n else 0.0
            rows.append(
                {
                    "scale_a": a,
                    "scale_b": b,
                    "n_a": len(set_a),
                    "n_b": len(set_b),
                    "intersection": len(inter),
                    "union": len(union),
                    "jaccard": len(inter) / len(union) if union else None,
                    "expected_intersection_if_independent": expected,
                    "observed_minus_expected": len(inter) - expected,
                    "intersection_blocks": sorted(inter),
                }
            )
    return rows


def build_convergence(
    flags_by_arm: dict[str, dict[str, set[str]]],
    evidence_by_arm: dict[str, dict[str, dict[str, Any]]],
    blocks: dict[str, dict[str, Any]],
    min_flags: int = 3,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for arm in CONFIGS:
        all_blocks = sorted({b for scale in SCALES for b in flags_by_arm[arm].get(scale, set())}, key=lambda b: blocks.get(b, {}).get("order_index", 999999))
        for block_id in all_blocks:
            scales = [scale for scale in SCALES if block_id in flags_by_arm[arm].get(scale, set())]
            if len(scales) < min_flags:
                continue
            block = blocks.get(block_id, {})
            rows.append(
                {
                    "arm": arm,
                    "block_id": block_id,
                    "order_index": block.get("order_index"),
                    "block_type": block.get("block_type"),
                    "flag_count": len(scales),
                    "flags": scales,
                    "source_excerpt": snippet(block.get("source_text", "")),
                    "S0_excerpt": snippet((block.get("outputs") or {}).get("S0", "")),
                    "S1_excerpt": snippet((block.get("outputs") or {}).get("S1", "")),
                    "evidence": {scale: evidence_by_arm[arm].get(scale, {}).get(block_id) for scale in scales},
                }
            )
    return rows


def check_chuan_hoa(
    flags_by_arm: dict[str, dict[str, set[str]]],
    evidence_by_arm: dict[str, dict[str, dict[str, Any]]],
    blocks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    cfg = "S1"
    decisions = load_json(ARTIFACTS["cascade_S1"])["decisions"]
    regularization_blocks: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        source_term = (decision.get("source_term") or "").casefold()
        if "regularization" not in source_term:
            continue
        rendered = rendered_surface(decision)
        target_text = decision.get("target_text") or ""
        contains_chuan_hoa = "chuẩn hóa" in norm_text(rendered) or "chuẩn hóa" in norm_text(target_text)
        if not contains_chuan_hoa:
            continue
        block_id = decision["block_id"]
        regularization_blocks[block_id] = {
            "block_id": block_id,
            "source_term": decision.get("source_term"),
            "rendered": rendered,
            "adherence_label": (decision.get("t3_code_score") or {}).get("adherence_label"),
            "accepted_forms": decision.get("accepted_forms") or [],
            "has_TA_Occ_flag": block_id in flags_by_arm[cfg]["TA-Occ"],
            "has_SF_BT_llm_flag": block_id in flags_by_arm[cfg]["SF-BT-llm"],
            "has_PJ_flag": block_id in flags_by_arm[cfg]["PJ"],
            "triad_hit": (
                block_id in flags_by_arm[cfg]["TA-Occ"]
                and block_id in flags_by_arm[cfg]["SF-BT-llm"]
                and block_id in flags_by_arm[cfg]["PJ"]
            ),
            "source_excerpt": snippet(blocks.get(block_id, {}).get("source_text", "")),
            "S1_excerpt": snippet((blocks.get(block_id, {}).get("outputs") or {}).get("S1", "")),
            "evidence": {
                scale: evidence_by_arm[cfg].get(scale, {}).get(block_id)
                for scale in ("TA-Occ", "SF-BT-llm", "PJ")
                if block_id in flags_by_arm[cfg].get(scale, set())
            },
        }
    triad = [row for row in regularization_blocks.values() if row["triad_hit"]]
    return {
        "query": "regularization rendered with 'chuẩn hóa' in S1 and flagged by TA-Occ + SF-BT-llm + PJ",
        "artifact_paths": [rel(ARTIFACTS["cascade_S1"]), rel(ARTIFACTS["sf_bt"]), rel(ARTIFACTS["pj"])],
        "regularization_chuan_hoa_blocks": sorted(regularization_blocks.values(), key=lambda r: blocks.get(r["block_id"], {}).get("order_index", 999999)),
        "triad_hit_count": len(triad),
        "triad_blocks": [row["block_id"] for row in sorted(triad, key=lambda r: blocks.get(r["block_id"], {}).get("order_index", 999999))],
        "passes_required_check": len(triad) > 0,
    }


def unique_contributions(
    flags_by_arm: dict[str, dict[str, set[str]]],
    evidence_by_arm: dict[str, dict[str, dict[str, Any]]],
    blocks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scale in SCALES:
        candidates: list[tuple[bool, int, str, str, list[str]]] = []
        for arm in CONFIGS:
            for block_id in flags_by_arm[arm].get(scale, set()):
                other = [s for s in SCALES if s != scale and block_id in flags_by_arm[arm].get(s, set())]
                candidates.append((len(other) == 0, blocks.get(block_id, {}).get("order_index", 999999), arm, block_id, other))
        if not candidates:
            rows.append({"scale": scale, "status": "no_flagged_blocks"})
            continue
        candidates.sort(key=lambda item: (not item[0], item[1], item[2], item[3]))
        unique, _, arm, block_id, other = candidates[0]
        block = blocks.get(block_id, {})
        rows.append(
            {
                "scale": scale,
                "arm": arm,
                "block_id": block_id,
                "unique": unique,
                "other_flags_same_arm": other,
                "source_excerpt": snippet(block.get("source_text", "")),
                "output_excerpt": snippet((block.get("outputs") or {}).get(arm, "")),
                "evidence": evidence_by_arm[arm].get(scale, {}).get(block_id),
                "artifact_path": (evidence_by_arm[arm].get(scale, {}).get(block_id) or {}).get("artifact_path"),
            }
        )
    return rows


def main() -> None:
    for path in ARTIFACTS.values():
        if not path.exists():
            raise SystemExit(f"Missing required artifact: {path}")

    metrics = load_json(ARTIFACTS["metrics"])
    sf_qe = load_json(ARTIFACTS["sf_qe"])
    sf_bt = load_json(ARTIFACTS["sf_bt"])
    pj = load_json(ARTIFACTS["pj"])
    blocks = load_block_texts(sf_bt)
    n_blocks = len(blocks)
    if n_blocks != 475:
        raise SystemExit(f"Expected 475 MLP blocks from SF-BT artifact, got {n_blocks}")

    tc_ta_summary, tc_ta_flags, tc_ta_evidence = compute_tc_ta_occ()
    sf_qe_flags, sf_qe_meta, sf_qe_evidence = build_sf_qe_flags(sf_qe)
    sf_bt_flags, sf_bt_meta, sf_bt_evidence = build_sf_bt_flags(sf_bt)
    pj_flags, pj_meta, pj_evidence = build_pj_flags(pj)

    flags_by_arm: dict[str, dict[str, set[str]]] = {cfg: {scale: set() for scale in SCALES} for cfg in CONFIGS}
    evidence_by_arm: dict[str, dict[str, dict[str, Any]]] = {cfg: {scale: {} for scale in SCALES} for cfg in CONFIGS}
    for cfg in CONFIGS:
        for scale in ("TC-Occ", "TA-Occ"):
            flags_by_arm[cfg][scale] = tc_ta_flags[cfg][scale]
            evidence_by_arm[cfg][scale] = tc_ta_evidence[cfg][scale]
        flags_by_arm[cfg]["SF-QE"] = sf_qe_flags[cfg]
        evidence_by_arm[cfg]["SF-QE"] = sf_qe_evidence[cfg]
        for scale in ("SF-BT-cos", "SF-BT-llm"):
            flags_by_arm[cfg][scale] = sf_bt_flags[cfg][scale]
            evidence_by_arm[cfg][scale] = sf_bt_evidence[cfg][scale]
        flags_by_arm[cfg]["PJ"] = pj_flags[cfg]
        evidence_by_arm[cfg]["PJ"] = pj_evidence[cfg]

    d2 = {
        cfg: {
            "N": n_blocks,
            "flag_counts": {scale: len(flags_by_arm[cfg][scale]) for scale in SCALES},
            "pairwise_overlap": pairwise_overlap(flags_by_arm[cfg], n_blocks),
            "flag_membership": {scale: sorted(flags_by_arm[cfg][scale]) for scale in SCALES},
        }
        for cfg in CONFIGS
    }

    convergence = build_convergence(flags_by_arm, evidence_by_arm, blocks)
    chuan_hoa = check_chuan_hoa(flags_by_arm, evidence_by_arm, blocks)
    unique = unique_contributions(flags_by_arm, evidence_by_arm, blocks)

    d1 = d1_summary(metrics, sf_qe, sf_bt, pj, tc_ta_summary)
    report = {
        "metric": "agreement-analysis",
        "metric_version": "agreement_analysis_mlp_v1",
        "phase": "official_stop_for_review",
        "status": "complete_no_api",
        "experiment_id": EXPERIMENT_ID,
        "chapter_id": CHAPTER_ID,
        "generated_from_git_head": git_head(),
        "scope": {
            "N_blocks": n_blocks,
            "configs": list(CONFIGS),
            "scales_for_D2": list(SCALES),
            "api_calls": 0,
        },
        "artifacts": {name: rel(path) for name, path in ARTIFACTS.items()},
        "D1_summary_7_scales": d1,
        "D2_block_agreement": d2,
        "D2_thresholds": {
            "SF-QE": sf_qe_meta,
            "SF-BT": sf_bt_meta,
            "PJ": pj_meta,
            "TC_TA_Occ": tc_ta_summary,
        },
        "D3_convergence_blocks_ge3_flags": convergence,
        "D3_chuan_hoa_required_check": chuan_hoa,
        "D4_unique_contributions": unique,
        "pre_registered_predictions_check": {
            "highest_overlap_expected": "TA-Occ↔TC-Occ",
            "chuan_hoa_expected_ge3_scales_on_S1": chuan_hoa["passes_required_check"],
            "PJ_intersection_SF_BT_expected_low": "see D2 pairwise overlap rows PJ vs SF-BT-cos/SF-BT-llm",
            "SF_QE_SF_BT_expected_moderate": "see D2 pairwise overlap rows SF-QE vs SF-BT-cos/SF-BT-llm",
        },
    }

    out_json = REPORT_DIR / "agreement_analysis_mlp.json"
    out_md = REPORT_DIR / "agreement_analysis_mlp.md"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    out_md.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print("STOP: no commit; offline-only agreement analysis complete.")


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Agreement Analysis — MLP")
    lines.append("")
    lines.append(f"- Status: `{report['status']}`")
    lines.append(f"- Experiment: `{report['experiment_id']}`")
    lines.append(f"- Chapter: `{report['chapter_id']}`")
    lines.append(f"- Scope: {report['scope']['N_blocks']} blocks × {', '.join(report['scope']['configs'])}")
    lines.append(f"- API calls: {report['scope']['api_calls']}")
    lines.append("")
    lines.append("## D1 — 7-Scale Summary")
    lines.append("")
    lines.append("| Scale | Reference frame | S0 | S1 | Δ S1-S0 | Conclusion |")
    lines.append("|---|---|---:|---:|---:|---|")
    for row in report["D1_summary_7_scales"]:
        if row["scale"] == "SF-BT":
            cos = row["subscores"]["SF-BT-cos"]
            llm = row["subscores"]["SF-BT-llm"]
            s0 = f"cos {cos['S0']:.4f}; llm {llm['S0']:.2f}"
            s1 = f"cos {cos['S1']:.4f}; llm {llm['S1']:.2f}"
            delta = f"cos {cos['delta_S1_minus_S0']:+.4f}; llm {llm['delta_S1_minus_S0']:+.2f}"
        else:
            s0_val = row.get("S0")
            s1_val = row.get("S1")
            delta_val = row.get("delta_S1_minus_S0")
            if row["scale"] == "PJ":
                s0 = f"{s0_val} wins"
                s1 = f"{s1_val} wins"
                delta = f"{delta_val:+}"
            else:
                s0 = f"{s0_val:.4f}" if isinstance(s0_val, float) else str(s0_val)
                s1 = f"{s1_val:.4f}" if isinstance(s1_val, float) else str(s1_val)
                delta = f"{delta_val:+.4f}" if isinstance(delta_val, float) else str(delta_val)
        lines.append(f"| {row['scale']} | {row['reference_frame']} | {s0} | {s1} | {delta} | {row['conclusion']} |")
    lines.append("")
    for row in report["D1_summary_7_scales"]:
        if row.get("proxy_warning"):
            lines.append(f"> **⚠ {row['scale']}:** {row['proxy_warning']}")
            lines.append("")
    lines.append("## D2 — Block-Level Flag Counts")
    lines.append("")
    for cfg, d2 in report["D2_block_agreement"].items():
        lines.append(f"### {cfg}")
        lines.append("")
        lines.append("| Scale | Flagged blocks |")
        lines.append("|---|---:|")
        for scale, count in d2["flag_counts"].items():
            lines.append(f"| {scale} | {count} |")
        lines.append("")
        lines.append("Top overlaps by observed-minus-expected:")
        lines.append("")
        lines.append("| Pair | Intersection | Expected if independent | Jaccard |")
        lines.append("|---|---:|---:|---:|")
        top = sorted(d2["pairwise_overlap"], key=lambda r: r["observed_minus_expected"], reverse=True)[:8]
        for row in top:
            jaccard = "n/a" if row["jaccard"] is None else f"{row['jaccard']:.3f}"
            lines.append(
                f"| {row['scale_a']} ∩ {row['scale_b']} | {row['intersection']} | {row['expected_intersection_if_independent']:.2f} | {jaccard} |"
            )
        lines.append("")
    lines.append("## D3 — Convergence Blocks (≥3 flags)")
    lines.append("")
    conv = report["D3_convergence_blocks_ge3_flags"]
    lines.append(f"Total convergence rows: **{len(conv)}**")
    lines.append("")
    for row in conv[:20]:
        lines.append(f"- `{row['arm']}` `{row['block_id']}` ({row['flag_count']} flags: {', '.join(row['flags'])})")
        lines.append(f"  - EN: {row['source_excerpt']}")
        lines.append(f"  - S0: {row['S0_excerpt']}")
        lines.append(f"  - S1: {row['S1_excerpt']}")
    if len(conv) > 20:
        lines.append(f"- ... {len(conv) - 20} more rows in JSON.")
    lines.append("")
    lines.append("## Required Check — `chuẩn hóa` Cluster")
    lines.append("")
    chk = report["D3_chuan_hoa_required_check"]
    lines.append(f"- Triad hit count: **{chk['triad_hit_count']}**")
    lines.append(f"- Passes required check: **{chk['passes_required_check']}**")
    lines.append(f"- Triad blocks: {', '.join('`' + b + '`' for b in chk['triad_blocks']) if chk['triad_blocks'] else '(none)'}")
    lines.append("")
    lines.append("## D4 — Unique Contribution Examples")
    lines.append("")
    lines.append("| Scale | Arm | Block | Unique? | Other flags | Artifact |")
    lines.append("|---|---|---|---:|---|---|")
    for row in report["D4_unique_contributions"]:
        if row.get("status") == "no_flagged_blocks":
            lines.append(f"| {row['scale']} | - | - | - | - | - |")
            continue
        other = ", ".join(row.get("other_flags_same_arm") or [])
        lines.append(
            f"| {row['scale']} | {row['arm']} | `{row['block_id']}` | {str(row['unique']).lower()} | {other or '-'} | `{row.get('artifact_path') or ''}` |"
        )
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    for name, path in report["artifacts"].items():
        lines.append(f"- `{name}`: `{path}`")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
