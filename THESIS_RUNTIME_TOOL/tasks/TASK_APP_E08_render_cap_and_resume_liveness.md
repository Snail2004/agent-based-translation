# TASK_APP_E08 — C1 render/state cap + C2 resume liveness guard

**Owner:** CodeX (implement) · **Gate:** Claude (independent verify on real behavior)
**Origin:** review of the E07-follow-up Console fixes on real run `run_e79867ab0ec9`
(S0+S1, completed, frozen hash `64D98965…B555C715` intact). The 5 UI fixes there
are correct; these two hardening items were flagged in that review.

---

## C1 — bound event rendering/state (avoid unbounded growth)

### Problem
The follow-up removed both caps:
- `app/prototype/app.jsx` (~line 1174): dropped `.slice(-1000)` on the accumulated
  event **state** array → state now grows without bound.
- `app/prototype/console.jsx` (~line 317): `const rendered = filtered.slice().reverse();`
  (was `filtered.slice(-220)`) → **every** event is rendered as a DOM row.

For the 271-event preface this is fine, but a full-chapter/book run (10k+ events)
will accumulate all events in state AND render all rows, re-rendering every ~1.4s
poll → memory bloat + jank. Note the app.jsx `-1000` removal was **not needed** for
the original bug (271 < 1000 already kept everything); the real culprit was only the
console.jsx `slice(-220)`.

### Constraint (do NOT regress)
Item #1 (absolute line numbers `#lineNo · a{attempt}/{seq}`) depends on
`deriveConsoleState` numbering `lineNo = idx + 1` over the **full** event history
from `run_start`. The RENDER cap must be applied AFTER numbering, so absolute
`lineNo` stays correct even when older rows are not shown.

### Fix
- **console.jsx (render cap — the important one):** cap the rendered rows, e.g.
  `const CONSOLE_RENDER_CAP = 2000;` then
  `const rendered = filtered.slice(-CONSOLE_RENDER_CAP).reverse();`
  Because `lineNo` is computed in `deriveConsoleState` BEFORE `filtered`, each row
  still shows its absolute line number. When rows are hidden, show a small dim hint
  (e.g. a first row / label): `… {filtered.length - CONSOLE_RENDER_CAP} dòng cũ hơn ẩn — dùng filter để thu hẹp`.
- **app.jsx (state cap — memory insurance):** re-introduce a **generous** cap instead
  of fully unbounded, e.g. `.slice(-20000)`. This keeps absolute `lineNo` correct for
  all realistic runs (retains from `run_start` well within the cap) while preventing
  pathological OOM. Do NOT go back to `-1000` (that re-loses long-run history).

### Acceptance (C1)
1. Reselect `run_e79867ab0ec9` (271 events): the FIRST event still shows `#1` (i.e.
   `#1-#51` are no longer lost), and line numbers are absolute/monotonic.
2. Synthetic/long-list check: with > CONSOLE_RENDER_CAP filtered events, the DOM row
   count is capped at CONSOLE_RENDER_CAP, the "N dòng cũ hơn ẩn" hint shows, and the
   newest row still carries the correct absolute `lineNo` (== total events).
3. No regression to item #1 numbering or the S0/S1 RESULTS panel.

---

## C2 — resume must not spawn a concurrent writer

### Problem
`app/backend/routes/thesis_runs.py:resume_thesis_run` (~line 419) builds the resume
argv and calls `spawn_run` reusing the **same** `run_dir`, `workdb`, `manifest`, and
`event_log` — with **no check that the prior process is dead**. The follow-up UI now
offers Resume for `st.paused || stalled` (plus a resume button in the stalled banner),
where `stalled` = backend still thinks the run is **running** (no events for 90s). If
that process is alive-but-hung, resuming spawns a **second** process writing the same
`workdb.sqlite3`/manifest → DB corruption / interleaved events / manifest races.

### Key facts (verified)
- A **paused** run's subprocess EXITS (run_one_button writes `manifest.status="paused"`
  and terminates at the stage boundary). So paused/failed/crashed → process is DEAD.
- Only an actively-running (or stalled-but-still-alive) run has a live pid.
- The registry entry carries `pid` (set at spawn, `services/thesis_runs.py:689`).
- No `psutil` dependency exists.

### Fix (backend is the authority — do not rely on UI gating alone)
Add a pid-liveness guard in `resume_thesis_run`, BEFORE `spawn_run`:
- Add a small helper `_is_pid_alive(pid) -> bool`:
  - `pid` falsy/None/0 → `False`.
  - Windows: `ctypes.windll.kernel32.OpenProcess(0x1000 /*PROCESS_QUERY_LIMITED_INFORMATION*/, False, pid)`;
    alive iff a non-null handle is returned (close it), else check via
    `tasklist /FI "PID eq {pid}"`. POSIX: `os.kill(pid, 0)` (alive unless
    `ProcessLookupError`; `PermissionError` counts as alive).
  - (Optional hardening: also confirm the live process is our python/run_one_button
    to reduce PID-reuse false positives; not required.)
- If `_is_pid_alive(entry.get("pid"))` → raise
  `RunControlError("run_still_active", "Run vẫn đang chạy; hãy Cancel trước rồi Resume.", 409)`.
- This correctly ALLOWS resume for paused/failed/crashed (dead pid) and REFUSES for a
  live process. Recovery for a truly-hung run: user clicks **Cancel** (does taskkill,
  sets status=cancelled), then **Resume** — now the pid is dead → allowed.
- PID-reuse caveat: a reused pid could false-positive "alive" → refuse → the same
  Cancel→Resume recovery applies (safe, recoverable).

### Optional UI polish (secondary, not the fix)
The stalled-banner resume + `canResumeRun` including `stalled` may now surface a 409.
Either surface the 409 message cleanly, or gate the UI resume to `runStatus==="failed"
|| st.paused` (drop `stalled`) and let Cancel handle hung runs. Backend guard remains
the source of truth regardless.

### Acceptance (C2)
1. Unit/route test: build a registry entry whose `pid` is a **live** process (e.g. the
   test process's own pid) with a resumable manifest → `POST /resume` returns 409
   `run_still_active`, and `spawn_run` is NOT called.
2. Same entry with a **dead** pid (e.g. an unused pid) → resume proceeds (spawn_run
   called), reusing the run_dir; no second concurrent process.
3. Manual: a paused run (subprocess exited) still resumes normally (regression check on
   the pause/resume feature).
4. Backend `pytest app/backend/tests/test_thesis_runs.py -q` stays green; add the two
   cases above.

---

## Files expected to change
- `app/prototype/console.jsx` (C1 render cap + hint)
- `app/prototype/app.jsx` (C1 state cap)
- `app/backend/routes/thesis_runs.py` (C2 guard + helper)
- `app/backend/tests/test_thesis_runs.py` (C2 tests)

## Reviewer notes (Claude)
- Verify C1 on the real path (reselect run_e79867ab0ec9 via natural selection, not
  synthetic `<select>` events — E06 lesson) and by pushing a > cap list.
- Verify C2 by the two route tests AND by confirming the live guard: with the current
  live run's pid, `_is_pid_alive` returns True and `/resume` 409s.
- Do NOT weaken the run_translate frozen-DB guard or the workdb `_work` relocation.
