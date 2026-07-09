"""A/B compare two Literary-Builder M1 runs (e.g. target_tokens 500 vs 1500).
Keys evidence by (block_id, surface) so it is INVARIANT to how blocks were
windowed — that is what lets us measure recall drop/gain across configs.
Usage: python compare_windows.py <run_dir_A> <run_dir_B> [labelA labelB]
"""
import json, glob, sys, os
from collections import Counter

def load_run(d):
    rep = json.load(open(os.path.join(d, "m1_report.json"), encoding="utf-8"))
    men, turns, events = set(), set(), set()
    men_res, turn_res = {}, {}   # (block,surface)->resolution detail
    for f in glob.glob(os.path.join(d, "lexicon", "*.json")):
        p = (json.load(open(f, encoding="utf-8")).get("parsed_json") or {})
        for m in p.get("character_mentions", []):
            for b in (m.get("block_ids") or [m.get("block_id")]):
                k = (b, (m.get("surface") or "").strip().lower())
                men.add(k)
                men_res[k] = f"{m.get('resolution_status')}/{','.join(m.get('candidate_entity_ids') or [])}"
    for f in glob.glob(os.path.join(d, "narrative", "*.json")):
        p = (json.load(open(f, encoding="utf-8")).get("parsed_json") or {})
        for t in p.get("speaker_turns", []):
            b = t.get("block_id")
            sp = t.get("speaker", {}) or {}
            k = (b, (sp.get("surface") or "").strip().lower(), t.get("address_term_used"))
            turns.add(k)
            a = t.get("addressee", {}) or {}
            turn_res[k] = f"spk={sp.get('resolution_status')}/{','.join(sp.get('candidate_entity_ids') or [])} adr={a.get('resolution_status')}/{','.join(a.get('candidate_entity_ids') or [])}"
        for e in p.get("relation_events", []):
            ac = e.get("actor", {}) or {}
            events.add((e.get("block_id"), (ac.get("surface") or "").strip().lower(), e.get("event_type")))
    return rep, men, turns, events, men_res, turn_res

def axis1(repA, repB, la, lb):
    print("=== AXIS 1: cost / calls / tokens ===")
    def row(name, rep):
        a = rep.get("actual", {}); vc = rep.get("validation_counts", {})
        nwin = sum(1 for _ in glob.glob(os.path.join("", "")) ) # placeholder
        print(f"  {name:6} calls={a.get('calls'):>4}  cost=${a.get('cost_usd',0):.4f}  in={a.get('prompt_tokens'):>7}  out={a.get('completion_tokens'):>6}  cached={a.get('cached_tokens', rep.get('actual',{}).get('cache_hits','?'))}  lex_ok={vc.get('lexicon_ok')}  nar_ok={vc.get('narrative_ok')}  nar_fail={vc.get('narrative_failed')}")
    row(la, repA); row(lb, repB)

def axis2(A, B, la, lb, kind):
    only_a = A - B; only_b = B - A; both = A & B
    print(f"  {kind:9}: {la}={len(A):>3} {lb}={len(B):>3} | shared={len(both):>3}  DROPPED_by_{lb}={len(only_a):>3}  GAINED_by_{lb}={len(only_b):>3}")
    return only_a, only_b

if __name__ == "__main__":
    A, B = sys.argv[1], sys.argv[2]
    la = sys.argv[3] if len(sys.argv) > 3 else "A"
    lb = sys.argv[4] if len(sys.argv) > 4 else "B"
    repA, menA, turnA, evA, mresA, tresA = load_run(A)
    repB, menB, turnB, evB, mresB, tresB = load_run(B)
    axis1(repA, repB, la, lb)
    print("\n=== AXIS 2: recall (keyed by block+surface, window-invariant) ===")
    dm = axis2(menA, menB, la, lb, "mentions")
    dt = axis2(turnA, turnB, la, lb, "turns")
    de = axis2(evA, evB, la, lb, "events")
    print(f"\n  DROPPED mentions (in {la}, not {lb}):")
    for k in sorted(dm[0], key=str)[:25]: print(f"    {k}  [{mresA.get(k,'')}]")
    print(f"  DROPPED turns (in {la}, not {lb}):")
    for k in sorted(dt[0], key=str)[:25]: print(f"    {k}  [{tresA.get(k,'')}]")
    print("\n=== AXIS 3: resolution changes on SHARED turns ===")
    chg = [(k, tresA[k], tresB[k]) for k in (turnA & turnB) if tresA.get(k) != tresB.get(k)]
    if not chg: print("  none (all shared turns resolve identically)")
    for k, ra, rb in chg[:30]: print(f"    {k}\n      {la}: {ra}\n      {lb}: {rb}")
