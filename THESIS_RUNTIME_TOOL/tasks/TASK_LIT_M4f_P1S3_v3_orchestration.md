# TASK_LIT_M4f_P1S3 — Builder v3 orchestration path (occurrence ground-state, no identity injection) — Phase 1, Step 3

Status: **DRAFT (Claude → Terra/CodeX), 2026-07-12.** Builds on Step-2 (ACCEPTED, `main` @ `8edac45`). Implementer = Terra. **Verify gate = Claude, adversarial + integration (NOT read+happy-path — see [[verify-gate-adversarial-probes]]).**

Contract source: `design/LITERARY_BUILDER_SCHEMA_ALLOCATION_V1.md` §1–§5 + §8, Canonical §2/§3/§9/§10.

---

## 0. Scope, reframe, non-scope (READ FIRST)

Step 3 does NOT rip out the legacy Builder. Per Sol's locked safe sequence, it **builds a parallel v3 orchestration path** that uses the Step-2 occurrence modules, with the identity-hint injections removed **inside the v3 path only**; **legacy stays untouched and DEFAULT**.

Crucial constraint: **real prompts still emit legacy schema (that is Step 6), so the v3 path cannot be driven by a real LLM run yet.** It is exercised end-to-end with **synthetic v3 payloads** (a deterministic stub that returns valid v3 JSON) — this proves the wiring, checkpointing, and reference closure WITHOUT new prompts or API calls.

IN scope:
- A v3 orchestration path selectable by an explicit flag (`builder_schema="v3"`), defaulting OFF (legacy).
- Occurrence ground-state store + M1V3/M2V3 checkpoint schema versions with hard-reject.
- Inside the v3 path: NO `seed_entity_ledger_from_chapter_brief`, NO `update_entity_ledger_from_lexicon`, NO registry/roster injection, NO `render_chapter_brief_for_injection` as a hint source.
- B0→B1→B2→B3 handoff wired per §3/§4 using Step-2 validators + code-mint; typed B0 projection to B3 (not the full brief).
- Synthetic-payload integration test + reference-closure + sentinel conformance.

OUT of scope (later): prompt rewrites (Step 6); deleting legacy code; switching the default to v3; any real 4-chapter LLM run; B4 identity/overlay. Do NOT touch the frozen D2L DB (`64D989…`), prompts, or the app-E12 files. No LLM/API/network. `sk-` scan before commit. Work on a branch; hand back for verify; do not self-certify.

---

## A. Deliverables

1. `pipeline/literary/builder_v3_pipeline.py` — the v3 orchestration path (M1V3 per-chapter B0→B1→B2, M2V3 B3), the occurrence ground-state, and a `SyntheticV3Model` stub interface used to drive it offline.
2. Additions (NOT edits to legacy behavior) in `builder_pilot.py` / `checkpoint.py`: `M1_CHECKPOINT_SCHEMA_VERSION_V3 = "literary_m1_checkpoint_v3"`, `M2_CHECKPOINT_SCHEMA_VERSION_V3 = "literary_m2_checkpoint_v3"`, and the `builder_schema` flag routing (default legacy).
3. `pipeline/tests/test_builder_v3_pipeline.py` — the integration + adversarial fixtures (§F).

The legacy driver, its checkpoint versions (`_v1`/`_v2`), and its default behavior are unchanged.

---

## B. v3 path per-stage contract (each stage: what it consumes, what it must NOT do)

Use the Step-2 `validate_*_v3` + `source_anchor` mint for every stage. After each stage, the payload stored in the ground-state is the **normalized `ValidationResult.payload`** (fatal → the window/chapter is marked failed, never silently emitted).

- **B0 (per chapter):** input = chapter block text ONLY. Runs `validate_chapter_brief_v3`. **MUST NOT** call `seed_entity_ledger_from_chapter_brief` or read any registry. Output cast_claims are surface×scene (Step-2 already enforces scene exact-cover + `source_block_ids ⊂ scene_range`).
- **B1 (per window):** input = active window + read-only context-only tail. Runs `validate_lexicon_v3`; code mints `mention_id`. **MUST NOT** inject a registry or call `update_entity_ledger_from_lexicon`.
- **B2 (per window):** input = window + tail + **B0 scene-intersecting projection** (scenes_party_size + cast surfaces for that window's scenes) + **B1 WINDOW_MENTIONS** (code-minted `mention_id`, surface, block). Runs `validate_narrative_v3`; code mints turn/event/endpoint ids. **MUST NOT** inject the entity roster.
- **B3 (per chapter):** input = full chapter + **typed B0 projection** (`setting` + `neutral_premise` as GIST_ONLY — NOT the full brief renderer) + B1 roster (surfaces only) + B2 events (compact) + the **K=2 prior chapters' rolling summaries** (separate, not cumulative). Runs `validate_digest_v3`.

Every stage's rendered request must pass the sentinel test (§F): a unique sentinel planted in each FORBIDDEN source (registry, roster, neighbor summaries, future windows/chapters, full-brief cast/role) never appears in the v3 rendered request.

---

## C. Occurrence ground-state + M1V3/M2V3 checkpoints

- **Occurrence ground-state** replaces the entity_ledger in the v3 path: a per-chapter structure holding the validated, id-minted v3 payloads (B0 claims, B1 mentions, B2 turns/events with endpoint ids, B3 digest) + the **reference index** (every `mention_id`/`endpoint_id`/`event_id` in scope). It holds **NO entity resolution, NO alias map, NO ledger** — identity is B4's, not built here.
- **M1V3 checkpoint** = the per-chapter B0+B1+B2 ground-state (+ `SourceAnchor`s + id maps). **M2V3 checkpoint** = the B3 digest + the **occurrence roster** (occurrence rows: `{mention_id | endpoint_id, surface, block_id, anchor}`) — NOT an entity roster.
- **Hard-reject by version (no silent migrate):** the v3 path stamps `schema_version = *_v3`; loading uses the existing `_checkpoint_expected` + `validate_checkpoint` machinery so a `_v1`/`_v2` checkpoint fails to load under v3 expectations and vice-versa. Old ch1–4 artifacts are a comparison **baseline**, never migrated. Probe this both directions.

---

## D. Reference closure (the invariant Step 3 must guarantee before Step 4/6)

After M1V3→M2V3 on a chapter, **every** reference resolves within scope: each `mention_ref` (B2) is a real B1 `mention_id` in the same window; each B3 `endpoint_refs`/`event_id`/`trigger_event_id`/`subject_ref(s)`/`event_ids` is a real minted id in the chapter's ground-state; no OLD (pre-remap) id survives a checkpoint round-trip. A closure check runs at M2V3 and fails the chapter if any reference dangles.

---

## E. Driving it offline (no prompts, no LLM)

Provide `SyntheticV3Model` returning deterministic valid v3 JSON per stage for a small synthetic chapter (reuse the Step-2 fixture style / block shapes). The v3 path takes an injected model object; the integration test wires `SyntheticV3Model` → full M1V3→M2V3 → checkpoint write → reload → closure check. This is how "the wiring works" is proven before Step 6 supplies real v3 prompts.

---

## F. Verify / acceptance (Claude re-runs adversarially)

1. **Legacy regression:** the existing legacy path is byte-for-byte behavior-unchanged — run the legacy dry-run/pilot fixtures and the full suite; all still green; frozen DB hash unchanged.
2. **v3 integration:** `SyntheticV3Model` → M1V3→M2V3 → checkpoint round-trip → **reference closure holds** (probe: plant an OLD id that should be remapped and assert it does not survive; break a `mention_ref` and assert the chapter fails).
3. **No-injection (sentinel):** plant a unique sentinel string in each forbidden source (registry, roster, neighbor summary, a future window, the full-brief cast/role) and assert it is ABSENT from every v3 rendered request; assert the v3 path never calls `seed_entity_ledger_*`/`update_entity_ledger_*`/roster injection (spy/patch or a call-log assertion).
4. **Checkpoint hard-reject:** write a v3 checkpoint, attempt to load it on the legacy (v1/v2) expectation → rejected; write a legacy checkpoint, load under v3 expectation → rejected. No silent migrate.
5. **Default safety:** with the flag unset, behavior is identical to today (legacy). The v3 path only runs when `builder_schema="v3"` is explicitly set.

Handback: diff, the integration + adversarial test run (green), the sentinel-absence proof, the closure proof, and confirmation the legacy suite + frozen DB are unchanged. Claude will independently re-run the closure probe, the sentinel test, and the cross-version checkpoint rejection before ACCEPT. Nothing switches the default or touches prompts until Step 6.
