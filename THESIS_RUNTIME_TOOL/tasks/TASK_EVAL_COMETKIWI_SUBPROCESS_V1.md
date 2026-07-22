# TASK_EVAL_COMETKIWI_SUBPROCESS_V1

Status: COMPLETE (0-API)

## Objective

Run the approved local `Unbabel/wmt22-cometkiwi-da` scorer from an explicit
Python 3.11 environment while the Evaluation orchestrator remains on its normal
Python runtime.

## Contract

- Parent sends only ordered `{src, mt}` rows, a batch size, and an exact local
  checkpoint path to a subprocess worker.
- The checkpoint path keeps its logical snapshot/symlink location instead of
  resolving to a bare content blob. COMET sidecars such as `hparams.yaml` are
  discovered relative to that snapshot path, while hashing and file checks
  still follow the symlink to the exact checkpoint bytes.
- The worker never downloads a model. A missing checkpoint fails before any
  scoring operation.
- The worker loads one pinned checkpoint, runs CPU inference, and returns only
  finite scores in `[0, 1]` under a closed JSON contract.
- The parent independently hashes the checkpoint and requires the worker's
  runtime description to report the same digest.
- The pilot precomputes exactly its selected SF-QE rows once, binds checkpoint,
  package, Python, CPU device, packet-set hash, and score-set hash into the
  execution artifact, then requires exact ordered consumption.
- A complete in-memory replay may reset and reuse that sealed batch without
  invoking the predictor again. A partial batch cannot be reset or published.
- Gold, human reference, arm identity, existing scores, thresholds, verdicts,
  report policy, source/runtime DB, and network credentials never enter the
  worker request.
- Worker stderr and exceptions are not relayed into persisted Evaluation
  evidence, preventing source text from leaking through diagnostics.
- The worker package must be `unbabel-comet` and the pilot device must be CPU;
  substitutions fail before scoring.

## Environment finding

- Default Python 3.13: `unbabel-comet` unavailable.
- Explicit Python 3.11:
  `C:\Users\nguye\AppData\Local\Programs\Python\Python311\python.exe`;
  `unbabel-comet 2.2.7`, `torch 2.12.1+cpu`, and `transformers 4.57.6` present.
- The `wmt22-cometkiwi-da` checkpoint is present in the offline Hugging Face
  cache under `D:\AI_Cache`. Its logical `model.ckpt` is a symlink and its
  SHA-256 is
  `4F357AA38B0737DCD502F166238C99711FF3419D7B5C8CDF9CDE08525A8E7858`.

## Verification

- COMET subprocess, pilot preparation, artifact lineage, exact-cover, and
  shared-backend integration probes: 30 passed.
- Evaluation regression: 327 passed. Final full `pipeline/tests` regression:
  1,340 passed in 336.55 seconds from the canonical runtime root.
- The full gate used a temporary read-only mirror of frozen DB SHA-256
  `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`.
  Source and mirror hashes remained identical; the mirror was removed.
- A real Python 3.11 `--describe` subprocess smoke test with a temporary fake
  checkpoint reported `unbabel-comet 2.2.7`, Python `3.11.9`, CPU, and the
  exact checkpoint SHA-256 without importing or loading a model.
- Snapshot-symlink regression plus COMET/local-SF-QE/pilot gates: `41 passed`.
- A real offline Python 3.11 smoke used the cached checkpoint through its
  logical snapshot path, reported the exact runtime/hash above, and scored one
  EN-VI row at `0.39840754866600037` without a download or provider call.
- API calls, network calls, model downloads, credential reads, and DB writes:
  zero.

## Non-goals

- No package installation, model download, Hugging Face authentication, API
  call, GPU policy, score aggregation, headline verdict, App wiring, or change
  to the accepted `LocalSfQeEvidenceV1` contract.
