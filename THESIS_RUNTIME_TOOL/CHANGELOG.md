# Changelog

## 0.8.0 - 2026-07-06

- O2 orchestrator: `run_one_button.py` (10-stage plan per J4, manifest resume with normalized argv_digest stage-skip, per-attempt stage event logs `.a<n>`, merged single-writer event log, per-stage estimate + cumulative budget gate, PAUSE file boundary pause, frozen-DB hash assert, multi-chapter guard, dynamic PJ --expected-db-sha256). RunControl: run_one_button allowlisted (estimate-only default), cancel endpoint POST /api/thesis/runs/<id>/cancel with taskkill /T /F, registry attempt fields, CREATE_NEW_PROCESS_GROUP on spawn. Claude review round 1 P0 fixes verified: SystemExit -> run_failed + manifest failed; translator resume skip; no re-emit of old-attempt events (0-API kill-resume pytest proves skip/no-dup/run_failed/attempt++).

## 0.7.0 - 2026-07-05

- N1-N5 blocking-debt pass: run_translate ro/workdb discipline test-locked; cascade arm_mode single_arm; re-election watchlist-only + gate_pause event; preflight_check CLI (LM Studio/keys/CometKiwi, machine-readable, no key values logged); RunControl allowlist + generic estimate-preview/confirm-token interface. Claude review fix: comet import timeout 20s -> 180s (false FAIL against correct py3.11 env).

## 0.6.0 - 2026-07-05

- UI-1: add one-button event schema v1 fixtures, replay driver, Agent Console, hardened event polling, and visible UI/API version badge.
