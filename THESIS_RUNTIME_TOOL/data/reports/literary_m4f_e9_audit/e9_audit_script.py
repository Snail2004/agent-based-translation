import json, os, re, sqlite3, hashlib
from collections import defaultdict

R = "THESIS_RUNTIME_TOOL/data/reports"
M4F = f"{R}/literary_m4_full"
B4 = f"{R}/literary_m4d_b4v2"
out = {}

def load(p):
    with open(p, encoding="utf-8") as f: return json.load(f)

# ---------- STAGE M1 ----------
m1 = {"briefs": 0, "lex_windows": 0, "nar_windows": 0, "mentions": 0, "glossary_cand": 0,
      "turns": 0, "events": 0, "drops": defaultdict(int), "endpoint_total": 0,
      "endpoint_no_atom_link": 0, "mention_hints": 0}
for f in sorted(os.listdir(f"{M4F}/brief")):
    if f.startswith("wh_"): m1["briefs"] += 1
for d, key in (("lexicon", "lex_windows"), ("narrative", "nar_windows")):
    for f in sorted(os.listdir(f"{M4F}/{d}")):
        if not f.startswith("wb_wh_"): continue
        m1[key] += 1
        pj = (load(f"{M4F}/{d}/{f}") or {}).get("parsed_json") or {}
        if d == "lexicon":
            ms = pj.get("character_mentions") or []
            m1["mentions"] += len(ms)
            m1["mention_hints"] += sum(1 for m in ms if m.get("candidate_entity_ids"))
            m1["glossary_cand"] += len(pj.get("glossary_candidates") or [])
        else:
            m1["turns"] += len(pj.get("speaker_turns") or [])
            evs = pj.get("relation_events") or []
            m1["events"] += len(evs)
            for e in evs:
                for role in ("actor", "target"):
                    ep = e.get(role) or {}
                    m1["endpoint_total"] += 1
                    # endpoints never carry source_atom_id (systemic)
                    if "source_atom_id" not in ep: m1["endpoint_no_atom_link"] += 1
rep1 = load(f"{M4F}/m1_report.json")
vc = rep1.get("validation_counts") or {}
m1["drops"] = {k: v for k, v in vc.items() if any(s in k for s in ("drop", "fail", "skip", "leak")) and v}
out["M1"] = dict(m1)

# ---------- STAGE M2 ----------
m2 = {"digests": 0, "frame_segments": 0, "rel_event_summary_rows": 0, "facts_claimed": 0}
for f in sorted(os.listdir(f"{M4F}/digest")):
    if not f.startswith("wh_"): continue
    m2["digests"] += 1
    pj = (load(f"{M4F}/digest/{f}") or {}).get("parsed_json") or {}
    m2["frame_segments"] += len(pj.get("narration_frame_segments") or [])
    m2["rel_event_summary_rows"] += len(pj.get("relation_event_summary") or [])
    m2["facts_claimed"] += len(pj.get("translator_relevant_facts") or [])
out["M2"] = m2

# ---------- STAGE M3 state ----------
m3 = {"chapters": [], "T2_entities": 0, "aliases": 0, "atoms": 0, "phases": 0, "facts": 0,
      "turns_T3": 0, "events_T3": 0, "review_only": 0, "blocked_pairs": 0, "address": 0}
defects = []  # candidate rows
ent_atoms_hints = []
ck4 = load(f"{B4}/checkpoints/m3_v2/wh_ch04.json")
ms4 = ck4["state"]["m3_state"]
def rows_of(x):
    if isinstance(x, dict): return list(x.values())
    return x or []
atoms_list = rows_of(ms4["atom_catalog"])
atom_by_id = {a["atom_id"]: a for a in atoms_list}
ents_list = rows_of(ms4["entities"])
a2e = ms4["atom_to_entity"]
for ch in ("wh_ch01", "wh_ch02", "wh_ch03", "wh_ch04"):
    sb = load(f"{B4}/story_bible_v2/{ch}_story_bible.json")
    row = {"ch": ch, "T2": len(sb.get("registry_T2_entities") or []),
           "turns": len(sb.get("registry_T3_speaker_turns") or []),
           "events": len(sb.get("registry_T3_relation_events") or []),
           "facts": len(sb.get("relation_facts") or []),
           "rels": len(sb.get("entity_relations") or []),
           "addr": len(sb.get("address_policies") or []),
           "blocked": len(sb.get("blocked_for_runtime_pairs") or []),
           "review_only": len(sb.get("review_only") or []),
           "frames": len(sb.get("narration_frame_segments") or [])}
    m3["chapters"].append(row)
# final-state sweeps on ch4 checkpoint (cumulative)
m3["T2_entities"] = len(ents_list)
m3["atoms"] = len(atoms_list)
m3["phases"] = len(rows_of(ms4["relation_phases"]))
m3["facts"] = len(rows_of(ms4["relation_facts"]))

# D1: entities whose member atoms carry conflicting/foreign hints
for e in ents_list:
    eid = e["entity_id"]
    hints = set()
    for aid in e.get("member_atom_ids") or []:
        h = (atom_by_id.get(aid) or {}).get("hint_entity_id")
        if h: hints.add(h)
    foreign = {h for h in hints if h != eid}
    if foreign:
        ent_atoms_hints.append({"entity": eid, "n_atoms": len(e.get("member_atom_ids") or []),
                                "foreign_hints": sorted(foreign)})
# D2: descriptor-surface entities (candidate epithet fragments) — flag, adjudicate by hand
desc_ents = []
for e in ents_list:
    cs = (e.get("canonical_surface") or e.get("display_surface") or "")
    if re.match(r"^(the|a|an|my|his|her|our|t'|owd)\b", cs.strip().lower()):
        desc_ents.append({"id": e["entity_id"], "surface": cs,
                          "kind": e.get("referent_kind"), "n": len(e.get("member_atom_ids") or [])})
# D3: relation facts with inference-marker notes (explicit-vs-derived)
inf_facts = []
for ch in ("wh_ch01", "wh_ch02", "wh_ch03", "wh_ch04"):
    sb = load(f"{B4}/story_bible_v2/{ch}_story_bible.json")
    for fct in sb.get("relation_facts") or []:
        note = (fct.get("predicate_note") or "").lower()
        if any(w in note for w in ("indicat", "suggest", "impli", "infer", "directs", "shows")):
            inf_facts.append({"ch": ch, "s": fct.get("subject_ref"), "p": fct.get("predicate_code"),
                              "o": fct.get("object_ref"), "q": (fct.get("evidence_quote") or "")[:60],
                              "note": (fct.get("predicate_note") or "")[:80]})
# D4: frames per chapter (flattening check)
frames = load(f"{B4}/story_bible_v2/wh_ch04_story_bible.json")["narration_frame_segments"]
# D5: dangling/foreign hint values that never became entities
all_eids = {e["entity_id"] for e in ents_list}
dangling_hints = defaultdict(int)
for a in atoms_list:
    h = a.get("hint_entity_id")
    if h and h not in all_eids: dangling_hints[h] += 1

out["M3"] = m3
out["D1_foreign_hint_entities"] = ent_atoms_hints
out["D2_descriptor_entities"] = desc_ents
out["D3_inference_marked_facts"] = inf_facts
out["D4_frames_final"] = frames
out["D5_dangling_hints"] = dict(sorted(dangling_hints.items(), key=lambda x: -x[1])[:15])
out["D6_blocked_review"] = {"blocked_pairs_ch4_bible": load(f"{B4}/story_bible_v2/wh_ch04_story_bible.json").get("blocked_for_runtime_pairs"),
                            "review_only_n": len(ms4.get("review_only") or [])}

# ---------- RAW coverage (M3) ----------
raw = {}
for ch in ("wh_ch01", "wh_ch02", "wh_ch03", "wh_ch04"):
    ck = load(f"{B4}/checkpoints/m3_v2/{ch}.json")
    listed = [os.path.basename(str(p)) for p in (ck.get("raw_responses") or [])]
    ondisk = set(os.listdir(f"{B4}/raw_responses/m3_v2/{ch}"))
    raw[ch] = {"listed_in_checkpoint": len(listed), "on_disk": len(ondisk),
               "listed_missing_on_disk": [p for p in listed if p and p not in ondisk]}
out["RAW_M3"] = raw

print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
