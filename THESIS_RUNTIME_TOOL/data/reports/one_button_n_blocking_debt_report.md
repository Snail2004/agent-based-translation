# One-button N-blocking-debt report

Date: 2026-07-05

Status: STOP for review, no commit.

## Scope

Implemented the 0-API N1-N5 blocker pass before the one-button orchestrator:

- N1: `run_translate` read-only source DB discipline and workdb-only writes.
- N2: cascade one-arm production mode contract.
- N3: re-election watchlist-only wiring with `gate_pause` event.
- N4: machine-readable preflight health-check CLI.
- N5: RunControl allowlist plus generic estimate-preview/confirm-token interface.

## Evidence

- Targeted tests: `python -m pytest pipeline/tests/test_run_translate_script.py pipeline/tests/test_experiment_cascade.py pipeline/tests/test_builder_v2_reelection.py pipeline/tests/test_preflight_check.py app/backend/tests/test_thesis_runs.py -q`
  - Result: `48 passed`
- Broad tests: `python -m pytest pipeline/tests app/backend/tests -q`
  - Result: `438 passed`
- Frozen DB hash after work: `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`

## N1

`run_translate._open_db` now delegates to explicit `_open_readonly_db` and `_open_writable_workdb`.

Tests prove:

- URI `mode=ro` rejects writes.
- Non-preflight path can write a marker table only to the workdb copy.
- The source DB hash is byte-identical before and after the non-preflight mocked run.

## N2

`run_experiment_cascade` now reports `arm_mode: single_arm | multi_arm`.

Tests prove:

- `_parse_configs("S1")` is accepted.
- `_parse_configs("S0,S1")` still behaves as before.
- Base report labels single-arm and multi-arm modes explicitly.

## N3

`builder_v2_reelection.py` accepts `--event-log`, `--run-id`, and `--attempt-id`.

Watchlist/preflight emits exactly one `gate_pause` event with:

- `watchlist_only: true`
- `artifact_path` pointing to `watchlist.json`
- watchlist size and call estimate

The test asserts no `notebook_reelected.json` is produced in watchlist-only preflight.

## N4

Added `pipeline/scripts/preflight_check.py`.

It checks:

- LM Studio `/v1/models` contains `gemma-4-12b`.
- LM Studio `/v1/models` contains `bge-m3`.
- OpenAI key exists and is non-empty via env/file, without logging value.
- Gemini key exists and is non-empty via env/file, without logging value.
- `comet` import succeeds in the configured Python.

It emits one `health_check` event per item and exits `0` on pass, `2` on fail.

## N5

RunControl now allowlists the one-button scripts and exposes `/api/thesis/runs/estimate-preview`.

The generic preview path:

- builds an estimate argv with the script dry-run flag where supported;
- builds the future real argv;
- issues a one-time confirm token bound to `job_id + script + exact argv digest`;
- returns `estimate_by_stage` as an interface placeholder for the orchestrator.

Current limitation: only scripts with an actual safe dry-run flag are estimate-preview supported now (`run_translate`, `run_experiment_cascade`, `builder_v2_reelection`). Scoring scripts remain allowlisted but API-capable calls stay blocked until they get their own estimate/preflight mode. This is intentional to avoid a false cost gate.

