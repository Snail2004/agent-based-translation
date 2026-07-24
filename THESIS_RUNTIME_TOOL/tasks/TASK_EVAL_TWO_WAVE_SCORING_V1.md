# TASK_EVAL_TWO_WAVE_SCORING_V1

Status: REVIEW

## 1. Objective

Implement the Evaluation-owned scoring schedule agreed for the five-chapter D2L
benchmark without changing any translation artifact:

- DTQ (`sf_qe`) over the complete eligible universe;
- TC-Occ and TA-Occ over the complete terminology-occurrence universe;
- BTF (`sf_bt`) and MTQ-5 (`pj`) over one shared deterministic two-wave sample;
- Wave A uses 50 five-block clusters (250 blocks);
- Wave B extends the exact Wave A prefix to 100 clusters (500 blocks) only when
  the preregistered uncertainty rule opens it.

The milestone is fixture-only and `0-API`. It provides a truthful runner and
replay/checkpoint boundary for later neutral-orchestrator integration. It does
not claim that the App production Score button is connected.

## 2. Scope

### In

- A deterministic, source-only, chapter-stratified sample manifest.
- Closed validators and self-hashes for the sample, work plan, uncertainty
  decision, sample coverage, MTQ-5 results, stage artifacts, and checkpoints.
- Exact five-arm identity and display-name catalogs.
- Exact method display-name catalog.
- A six-dimension, blinded, both-orientation MTQ-5 packet and aggregator.
- A two-wave runner over `EvaluationWorkflowComponentWriterV1`.
- Fail-closed preflight, immutable stage artifacts, halt/Resume, terminal
  replay, and conditional Wave B stages.
- Focused fixture and adversarial tests.

### Out

- Provider calls, credentials, live quota use, model selection, or fallback.
- Re-running or modifying D2L translations.
- App/backend, neutral relay, D2L, Literary, SQLite, source-package, or public
  report-contract changes.
- A cross-method synthetic composite score.
- Replacing missing sample units or silently reducing the five-arm benchmark.
- Wiring concrete DTQ, BTF, TC-Occ, or TA-Occ provider executors into the
  production App launcher.

## 3. Locked Design

### 3.1 Arms and methods

Stable arm IDs remain:

| Arm ID | Display name |
|---|---|
| `S0` | ABT-Base |
| `S1` | ABT-Context |
| `community` | D2L-Community |
| `google_nmt` | Google-Translate |
| `llm_lc` | LLM-BookContext |

Stable method IDs remain:

| Method ID | Display name |
|---|---|
| `sf_qe` | DTQ |
| `sf_bt` | BTF |
| `pj` | MTQ-5 |
| `tc_occ` | TC-Occ |
| `ta_occ` | TA-Occ |

### 3.2 Sampling

The chapter order and cluster quotas are fixed:

| Chapter | Wave A clusters | Wave B cumulative clusters |
|---|---:|---:|
| `d2l_preliminaries` | 11 | 22 |
| `d2l_linear_neural_networks` | 11 | 21 |
| `d2l_multilayer_perceptrons` | 15 | 30 |
| `d2l_deep_learning_computation` | 6 | 13 |
| `d2l_convolutional_neural_networks` | 7 | 14 |

Sampling uses source identity, source order, declared seed, and optional sealed
source-only term-count features. It never reads translations, scores, gold,
oracle data, arm identity, or prior findings. Wave B preserves Wave A as an
exact ordered prefix.

### 3.3 Wave decision

Wave B opens when at least one registered paired 95% confidence interval
includes zero, or when BTF and MTQ-5 disagree in direction for a pair.
Otherwise Wave B is skipped and no Wave B semantic executor is invoked.
Unresolved comparisons after Wave B remain `INCONCLUSIVE`.

BTF and MTQ-5 each emit a closed cluster-level stage payload. Wave A payloads
cover exactly the frozen 50 clusters; Wave B payloads cover only the exact 50
additions. Every cluster covers the same ten arm pairs. Code validates and
content-addresses those stage artifacts, then derives each paired mean and
normal-approximation 95% confidence interval from the accepted cluster deltas.
The decision records the sampling manifest, coverage hashes, component/settings
identity, and exact scorer-artifact hashes. No caller may submit precomputed
confidence intervals or a decision.

### 3.4 Coverage

The same frozen block IDs apply to all five arms and both sampled methods.
Every selected row must be present and translated. Missing, failed, foreign, or
duplicate rows block the run before any scorer. No replacement unit is minted.

### 3.5 MTQ-5

Each sampled five-block cluster is judged for every unordered arm pair in both
candidate orientations. The judge returns six integer 1-5 dimensions for each
candidate:

- adequacy;
- faithfulness;
- terminology;
- coherence;
- style;
- overall.

Arm IDs, producer names, and baseline labels are absent from the prompt.
Orientation is mapped back to stable arm IDs in code. Each method is reported
separately; MTQ-5 is not averaged with DTQ, BTF, TC-Occ, or TA-Occ.

### 3.6 Replay and checkpoints

The runner emits the closed 12-stage component schedule. Stage output is
content-addressed and bound to the sampling manifest plus settings hash. A
completed or skipped stage is loaded from its immutable artifact on Resume and
is not re-executed. Component attempt identity may advance while
`component_run_id` remains stable. Terminal replay revalidates the BTF/MTQ-5
payloads and rederives both uncertainty decisions from their exact artifacts.

## 4. Acceptance

- Deterministic replay produces byte-identical sample IDs and hashes.
- Wave A contains exactly 50 clusters/250 blocks and Wave B exactly
  100 clusters/500 blocks, with Wave A as the exact prefix.
- Chapter quotas, five-arm order, and method catalogs are closed.
- Translation changes cannot alter the sample.
- Missing coverage in either wave halts before any scorer callback.
- Wave B opens only from confidence intervals and method directions rederived
  from exact BTF/MTQ-5 artifacts.
- Wave A decisions require 50 units for all ten pairs; Wave B decisions require
  100 cumulative units with the exact Wave A prefix.
- Wave B executors receive only the 50 incremental clusters.
- Identical scorer artifacts cannot produce different uncertainty decisions.
- Terminal replay makes zero scorer calls.
- Tampered stage artifacts fail closed.
- A real `EvaluationWorkflowComponentWriterV1` package validates.
- Injected failure resumes under a new component attempt without re-running
  accepted stages.
- Tests, compilation, diff check, and secret scan pass with zero API calls.

## 5. Implementation Handback

### 5.1 Files

- `pipeline/eval/two_wave_sampling_v1.py`
- `pipeline/eval/two_wave_coverage_v1.py`
- `pipeline/eval/mtq5_v1.py`
- `pipeline/eval/two_wave_runner_v1.py`
- `pipeline/tests/test_evaluation_two_wave_sampling_v1.py`
- `tasks/TASK_EVAL_TWO_WAVE_SCORING_V1.md`

### 5.2 Public callables

- `build_two_wave_sampling_manifest_v1(...)`
- `validate_two_wave_sampling_manifest_v1(...)`
- `build_two_wave_method_stage_payload_v1(...)`
- `validate_two_wave_method_stage_payload_v1(...)`
- `build_two_wave_work_plan_v1(...)`
- `build_two_wave_uncertainty_decision_v1(...)`
- `validate_two_wave_uncertainty_decision_v1(...)`
- `build_two_wave_sample_coverage_v1(...)`
- `prepare_mtq5_items_v1(...)`
- `parse_mtq5_response_v1(...)`
- `aggregate_mtq5_results_v1(...)`
- `run_two_wave_scoring_v1(...)`

### 5.3 Verification

Focused two-wave contract, runner, writer, Resume, and adversarial tests:

```text
26 passed in 15.74s
```

Adjacent two-wave/writer/benchmark/canonical-bridge regression:

```text
51 passed in 53.18s
```

All 48 `test_evaluation_*.py` files:

```text
577 passed in 466.19s (0:07:46)
```

Python compilation passed for all four modules and the focused test file. File
hygiene passed with LF endings, final LF, and no trailing whitespace.
`git diff --check` and the credential-pattern scan passed.

### 5.4 Runtime and data effects

- API/provider calls: `0`
- Credential reads: `0`
- SQLite reads/writes: `0` / `0`
- Source, translation, App, relay, checkpoint, and report-root mutation outside
  pytest temporary roots: `0`
- Git commit/push: `0` (reviewer owns commit)

### 5.5 Known gap

This milestone exposes the Evaluation runner and its exact writer/replay
contract. The production App/neutral orchestrator still needs a separately
reviewed binding from the registered DTQ/BTF/TC-Occ/TA-Occ/MTQ-5 executors to
these stage callbacks. Until that integration gate passes, the App Score button
must not claim this two-wave policy is live.
