# TASK_APP_E07 — Console live-poll must survive the `pending` window after DỊCH

**Owner:** CodeX (implement) · **Gate:** Claude (independent verify on real behavior)
**Priority:** High UX (no data/cost loss; one-button appears dead until manual reload)
**Depends on:** none. Independent of the workdb-path fix.

## Symptom (reproduced by user)
Click **DỊCH** → new run spawns in the backend, but Console stays at
`status=pending / events=0 / stream=closed` and never updates. Only a hard
`Ctrl+F5` makes it hydrate to the real state (e.g. `failed`, `events=40`,
translator failed). `Ctrl+F5` does not cause the failure — it only forces the
frontend to re-read real state.

## Root cause (CONFIRMED by Claude on real code — NOT a stale-tab artifact)
Causal chain, all in current code:
1. `POST /thesis/runs` registers the run with `status="pending"`
   (`app/backend/services/thesis_runs.py:221`) and returns that status.
2. `read_log` / `read_events` compute `"running": entry["status"] == "running"`
   (`services/thesis_runs.py:769` and `:820`).
3. Status flips to `"running"` only **inside the spawn thread**, after the
   subprocess starts (`services/thesis_runs.py:689`).
4. Frontend poll (`app/prototype/app.jsx:1188`) re-schedules the next poll
   **only** when `result.running || eventResult?.running || needsPartialFollowup`.
5. **Race:** the effect's first `poll()` fires immediately on `setSelectedRunId`.
   If it lands before step 3, both endpoints return `running=false` while
   `status="pending"`. Line 1188 is false → **no timer scheduled → the poll loop
   terminates and never recovers.** The run keeps executing in the backend
   (events accumulate, then translator fails), but the UI is frozen at
   `events=0`. `Ctrl+F5` → `selectedRunId` resets → the auto-pick effect
   (`app.jsx:1204`) re-selects the now-active/terminal run → a fresh poll sees
   events + terminal status → renders correctly.

Hypothesis #1 (stale tab) is NOT required; the race reproduces on fresh code.

## Fix (frontend only — do NOT change backend `running` semantics)
`running` (== process alive) is used elsewhere (cancel gating, labels); leave it.
Change the poll **continuation** to be driven by *non-terminal status*, not by
`running`.

In the poll effect (`app.jsx`, around lines 1184–1190):
- Define terminal statuses: `TERMINAL = {"done","failed","cancelled","error"}`.
  (Note: `paused` is a pause-**file**, not a registry status — a paused run keeps
  `status="running"`; do not add it here.)
- Derive `const status = (eventResult?.status || result.status || "");`
- Re-schedule logic:
  - `needsDrain` (truncated) → `setTimeout(poll, 0)` (unchanged).
  - else if **not** terminal → `setTimeout(poll, needsPartialFollowup ? 600 : 1400)`.
    This keeps polling through `pending → running → terminal`, and through the
    empty-status race window (`""` is not terminal).
  - else (terminal) → stop (no timer).
- Keep the existing `cancelled` flag and the `prev.run_id === selectedRunId`
  guards exactly as-is (they prevent stale cross-run writes — do not regress).

### Display
`stream=closed` / any "dead" indicator must render only when `status` is
terminal. While `status ∈ {"", pending, running}` show a live/connecting state,
not `closed`. (`events=0` alone must never render as closed.)

## Must NOT regress
- **Completed run selected from picker / auto-pick:** `status` is terminal →
  poll runs once, hydrates all events, then stops (no infinite polling on done
  runs). This must still hydrate correctly (the E06/auto-pick real path).
- **Switching runs mid-poll:** no stale response may overwrite the newly
  selected run's state (existing guards must remain effective).
- No busy-loop: a normal terminal run stops polling within one cycle of
  reaching terminal.

## Acceptance (verify with REAL behavior, not just green unit tests)
1. **Deterministic replay/fake-API test** (add to the app/back-end test that
   already fakes the runs API):
   - poll1 returns `{status:"pending", running:false, events:[]}`
   - poll2 returns `{status:"running", running:true, events:[run_start]}`
   - poll3 returns `{status:"failed", running:false, events:[...]}`
   - Assert: the loop **continues after poll1** (does not stop at pending), the
     `run_start` event is shown after poll2, and the loop **stops after poll3**.
2. **Live, no reload:** start backend, open `thesis:d2l_p1` → Console → click
   DỊCH → **without any Ctrl+F5**, Console must move on its own from
   pending → first events appearing → terminal. Capture the transition.
3. Selecting an existing completed run still hydrates (events + arms badge +
   RESULTS) and does not poll forever.
4. Backend `running` semantics unchanged; no new backend behavior.

## Notes for the reviewer (Claude)
- Verify on the real path (natural DỊCH click / auto-pick), NOT via
  `preview_fill`/synthetic `change` on the controlled `<select>` — that path
  gives false negatives (see the E06 phantom-bug lesson).
- Confirm the fix by A/B: current code stops at pending; patched code continues.
