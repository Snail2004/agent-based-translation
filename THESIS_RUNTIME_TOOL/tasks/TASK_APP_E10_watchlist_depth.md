# TASK_APP_E10 — Watchlist §36 depth: why-flagged + candidates + review state (gap E)

**Owner:** CodeX (implement) · **Gate:** Claude (independent verify on real artifacts)
**Value:** Medium-high. Tells the §36 belief-revision story (canonical held pending
human review) instead of teasing it. Currently the panel shows only
`source_term → canonical` + "N pending".
**Cost:** LOW. **Frontend only (console.jsx)** — the `/watchlist` endpoint already
returns full entries; no backend/re-run needed.

## What the payload already carries (verified on real run `run_e79867ab0ec9`)
The endpoint returns the raw `artifacts/reelection/watchlist.json` list. Each entry
(verified fields):
- `source_term`, `canonical_target_vi` (the held canonical)
- `audit_label` (e.g. `polysemy_or_context_dependent`) and `watchlist_reasons`
  (e.g. `["audit_polysemy"]`) — **why it was flagged**
- `injection_action` (e.g. `context_sensitive_translate`) — **how it's handled**
- `candidates` / `competitors`: list of `{ text, source ("canonical"|"target_variant"),
  evidence_block_id, variant_reason }` — **the competing renderings**
- `collision_soft_fallback` (null here; non-null for collision-type)
- `evidence_blocks`, `evidence_block_ids`, `backtranslation_calls` — evidence/cost

Real examples:
- `framework → khung phần mềm` · polysemy · context-sensitive · candidates:
  khung phần mềm (canonical), khuôn khổ (variant @b007), khung phần mềm học sâu
  (variant @b028), khung phần mềm nguồn mở (variant @b030) · 4 evidence blocks · 4 BT calls
- `training → huấn luyện` · polysemy · candidates: huấn luyện (canonical),
  đào tạo (variant)

## Current code (to replace)
`console.jsx` around the `:: watchlist §36` section renders per entry only:
`source_term → canonical_target_vi` (truncated 16). Keep the existing defensive
field fallbacks (`w.term || w.source_term …`) for cross-run robustness.

## Fix (console.jsx only)
Enrich each watchlist row (keep it compact; the panel lists up to 8 entries). Per entry:
1. **Header:** `source_term → canonical_target_vi` (give the canonical more room than 16
   chars, e.g. ~28).
2. **Why-flagged chip:** map `audit_label` / `watchlist_reasons` to a short friendly
   label, e.g. `polysemy_or_context_dependent`/`audit_polysemy` → "polysemy · ngữ cảnh";
   a collision label (audit_label containing "collision" or `collision_soft_fallback`
   non-null) → "collision". Unknown → show the raw `audit_label`. Tone: `kv-warn`.
3. **Injection action:** map `injection_action`, e.g. `context_sensitive_translate` →
   "soft · do-not-force"; `hard_translate` → "mandatory"; else raw. `kv-dim`.
4. **Competing candidates:** a compact line naming the competitors — mark the canonical
   distinctly and show up to 3 variants with their `evidence_block_id`, then "+N more".
   e.g. `canonical: khung phần mềm · vs: khuôn khổ (b007), khung phần mềm học sâu (b028) +1`.
   Prefer `competitors` (variants only) if present, else derive from `candidates`
   (source != "canonical").
5. **Evidence:** small dim suffix `· 4 blocks / 4 BT` from `evidence_blocks` /
   `backtranslation_calls` when present.
6. **Review state:** these are held pending human review — the §36 flip applies only
   after review. There is **no** persisted per-entry approved/rejected field, so keep
   the header count as "N pending" / "held" and do NOT invent a status. (If a future
   human-review workflow adds `review_status`, surface it then.)

Styling: reuse console classes (`watch-row`, `watch-term`, `watch-arrow`, `watch-vi`,
`kv-dim`, `kv-warn`, `section-label`). Keep it readable at the aside width; long forms
use the existing `consoleShort` truncation.

## Acceptance (verify on REAL data, not just compile)
1. Live on `run_e79867ab0ec9`: the panel shows both entries with
   - `framework` → polysemy · soft/do-not-force · competitors khuôn khổ (b007),
     khung phần mềm học sâu (b028), khung phần mềm nguồn mở (b030) · 4 blocks / 4 BT
   - `training` → polysemy · competitors đào tạo
   matching `artifacts/reelection/watchlist.json` (reviewer recomputes from the file).
2. Degraded/cross-run: an entry missing `audit_label`/`candidates` still renders the
   header without throwing (defensive access); an empty watchlist still shows the
   existing "trống — nối sau bước re-election" line.
3. Frontend compiles clean (no Babel errors); no backend change; frozen hash unchanged.

## Reviewer notes (Claude)
- No backend edit expected — if CodeX changes the endpoint, verify it didn't trim
  fields the panel needs.
- Confirm on the natural selection path (not synthetic `<select>` — E06 lesson).
- Keep the framing honest: watchlist entries are *held for human review*
  ([[weighted-ledger-promotion-three-gate]], [[canonical-reelection-design-v2]]);
  polysemy holds are correct behavior, not errors.
