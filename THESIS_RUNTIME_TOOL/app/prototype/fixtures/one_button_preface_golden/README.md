# One-button golden fixture — d2l_preface (real run, 2026-07-06)

Captured from the first real end-to-end [DICH] run (B6). Replay this to build/iterate
Console UI + animations at $0 instead of re-running the paid pipeline.

## Contents
- `events.jsonl` — the merged one-button event log (134 events, run_id=b6_real).
  This drives EVERYTHING the Console shows: stage checklist, progress, cost snapshots,
  gate/heartbeat, phase-1/phase-2 boundary.
- `manifest.json` — resume/status source of truth (stage table, per-stage estimate).
- `block_preview_sample.json` — 10 real EN->VI translated blocks for the preview panel.
  NOTE: production translations live in workdb table `translation_runs.output_text`
  (NOT `translation_records`, which is empty). Block-preview fetch must target that.
- `reports/translate.json` `reports/sf_bt.json` `reports/watchlist.json` — Phase-1/2
  report-render inputs.

## Replay
Instant (static layout):
  python -m app.backend.scripts.replay_thesis_events --fixture app/prototype/fixtures/one_button_preface_golden/events.jsonl --run-id replay_preface --instant

Timed (animation/typewriter dev, adjustable speed):
  python -m app.backend.scripts.replay_thesis_events --fixture app/prototype/fixtures/one_button_preface_golden/events.jsonl --run-id replay_preface --speed 5

The Console cannot tell replay from a live run: same events endpoint + manifest shape.

## Frozen artifact — do not "fix"
This is a recording, not live data. It is intentionally frozen at the shapes emitted by
v0.8.0. If the UI later needs an event type/field this run did not emit, capture again or
add a synthetic line (see pipeline/scripts/one_button_event_fixtures.py for the 20/20 set).
