#!/usr/bin/env python3
"""Stage C3 — render REVIEW-ONLY term cards for the Term-Auditor (d2l_term_audit_v1).

Read-only reproducer so the locked card schema (TASK_BUILDER_V2 §23.1) is reviewable
without running any API. The PRODUCTION card-builder is CodeX's §5 driver; this script
only proves the schema renders and gives reviewers real cards.

BLIND-TO-GOLD: reads ONLY blocks.text from the frozen DB. Never touches
eval_glossary_gold / reference_eval_only.

Usage:
  python THESIS_RUNTIME_TOOL/pipeline/scripts/builder_v2_c3_sample_cards.py \
      --notebook THESIS_RUNTIME_TOOL/data/reports/builder_v2_c2_pilot/notebook.json \
      --db       THESIS_RUNTIME_TOOL/data/jobs/d2l_p1/memory.sqlite3 \
      --terms norm shape gradient one example arange linalg.norm circle
"""
import argparse, json, re, sqlite3

MATH_CODE = re.compile(r"[=${}\\]|\d")  # surface looks like math/code, not a lexical term


def _block(cur, bid):
    r = cur.execute(
        "SELECT text, chapter_id, block_type FROM blocks WHERE block_id=?", (bid,)
    ).fetchone()
    return (r[0] or "", r[1] or "", r[2] or "") if r else ("", "", "")


def _snippet(text, surfaces, win=45):
    t = re.sub(r"\s+", " ", text).strip()
    low = t.lower()
    pos = -1
    for s in surfaces:
        pos = low.find(s.lower())
        if pos >= 0:
            break
    w = t.split()
    if pos < 0:
        return " ".join(w[:win]) + (" …" if len(w) > win else "")
    upto = len(t[:pos].split())
    a = max(0, upto - win // 2)
    b = min(len(w), a + win)
    return ("… " if a > 0 else "") + " ".join(w[a:b]) + (" …" if b < len(w) else "")


def _all_ev_ids(e):
    ids = []
    for c in e["conflict_ledger"]:
        ids += c.get("evidence_block_ids", [])
    for sv in sorted(e["source_variants"], key=lambda s: -s.get("occurrence_count", 0)):
        ids += sv.get("evidence_block_ids", [])
    for d in e["decision_log"]:
        ids += d.get("evidence_block_ids", [])
    seen = []
    [seen.append(x) for x in ids if x not in seen]
    return seen


def _surfaces(e):
    return [sv["surface"] for sv in e["source_variants"]] or [e["canonical_source_term"]]


def build_card(cur, e):
    cs = e["canonical_source_term"]
    ids = _all_ev_ids(e)
    chaps = {_block(cur, b)[1] for b in ids if _block(cur, b)[1]}
    has_conf = bool(e["conflict_ledger"])
    multivar = len(e["target_variants"]) > 1
    n_ev = 2 if (has_conf or multivar) else 1

    # FIX #5: prose-only, NO fallback to code/math. No prose -> empty + reason.
    prose_ids = [b for b in ids if _block(cur, b)[2] == "prose"]
    evidence, missing = [], None
    if prose_ids:
        for b in prose_ids:
            tx = _block(cur, b)[0]
            if tx.strip():
                evidence.append(_snippet(tx, _surfaces(e)))
            if len(evidence) >= n_ev:
                break
    else:
        missing = "no prose occurrence"

    # FIX #3: mechanical over-merge suspicion (concept_key is not a true stable id).
    # Flag only when a surface looks like a DIFFERENT concept merged in: an equation/
    # latex token, OR math/code-ish AND missing the canonical word. A descriptive form
    # that still contains the term (e.g. "shape (2, 3, 4)") is NOT an over-merge.
    plain_head = bool(re.fullmatch(r"[A-Za-z][A-Za-z .\-]*", cs.strip()))
    cs_low = cs.strip().lower()

    def _alien(s):
        sl = s.lower()
        if "=" in s or "$" in s:
            return True
        return bool(MATH_CODE.search(s)) and cs_low not in sl

    overmerge = plain_head and any(_alien(s) for s in _surfaces(e))

    flags = []
    if re.search(r"[.\d]", cs) or e["do_not_translate"]:
        flags.append("code_or_symbol_like")

    card = {
        "entry_id": e["concept_key"],          # PILOT key only; Phase D needs stable id
        "source_term": cs,
        "surface_variants": _surfaces(e)[:8],
        "builder_proposed_vi": e["canonical_target_vi"],
        "builder_target_variants": [tv.get("text") for tv in e["target_variants"]][:2],
        "_note": "builder_proposed_vi/variants are MODEL-GENERATED notes, NOT gold/reference",
        "signals": {
            "occurrences_total": e["occurrences_total"],
            "chapter_spread": len(chaps),
            "is_multiword": " " in cs.strip(),
            "do_not_translate": e["do_not_translate"],
            "has_conflict": has_conf,
            "n_target_variants": len(e["target_variants"]),
            "surface_flags": flags,
            "overmerge_suspected": overmerge,
        },
        "evidence": evidence,
        "evidence_truncated": len(prose_ids) > n_ev,
    }
    if missing:
        card["evidence_missing_reason"] = missing
    return card


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notebook", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default="-")
    ap.add_argument("--terms", nargs="*", default=[
        "norm", "shape", "gradient", "one", "example", "arange", "linalg.norm", "circle"])
    a = ap.parse_args()

    nb = json.load(open(a.notebook, encoding="utf-8"))
    by = {e["canonical_source_term"].lower(): e for e in nb["entries"]}
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    cur = con.cursor()
    cards, missing = [], []
    for t in a.terms:
        e = by.get(t.lower())
        if e is None:
            missing.append(t)
            continue
        cards.append(build_card(cur, e))
    con.close()
    out = json.dumps(cards, ensure_ascii=False, indent=1)
    if a.out == "-":
        print(out)
    else:
        open(a.out, "w", encoding="utf-8").write(out)
        print(f"wrote {len(cards)} cards -> {a.out}")
    if missing:
        print("ABSENT in notebook:", missing)


if __name__ == "__main__":
    main()
