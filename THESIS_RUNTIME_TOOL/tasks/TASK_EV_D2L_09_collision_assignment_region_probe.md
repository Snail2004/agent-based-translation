# TASK EV-D2L-09 — Ambiguous-assignment probe (true-collision + variant-stealing) — deterministic vs position vs embedding region-narrowing

Status: REVIEW (CodeX implemented REWORK-2/REWORK-2b additions; gold still unlabeled so production probe correctly stops before embeddings) -> Claude reviews sec 6 + commits.
Type: EVAL PROBE / method-decision. eval-only, deterministic output, embeddings cached+frozen. NO change to the headline metric.

### REWORK-2 DELTA (what to add vs the REVIEW build — everything else verified correct, keep it)
1. **Add a 4th, MODEL-FREE reference `position_narrow`** (monotonic relative-position sentence narrowing). Reason: the two existing baselines both pick `candidates[0]` (position-blind), so `region_narrow` could win merely by NARROWING — not by embeddings. Without a free position baseline the probe cannot answer "is the model worth its cost". `position_narrow` is the measuring stick; an embedding model is justified ONLY if it beats it. **Adding this needs NO re-labeling** — gold is (source_occ -> correct span); a new reference is just another decision function scored on the same gold.
2. **Make the embedding model a COMPARISON DIMENSION**: run `region_narrow` with each of `{LaBSE, BGE-M3, multilingual-e5-large}` (LM Studio, Q8_0/f16). Decide the winner on the HARD subset, not on reputation/MTEB. `region_align` is already model-agnostic (cache key includes model_id) so this is a loop, not a rewrite. **E5 REQUIRES `query:` / `passage:` prefixes** — add per-model prefix config or E5 is unfairly handicapped.
3. **Record the REAL model identity** (HF repo + quant) in the cache key + report, not the constant `model_version="lmstudio-local"` — otherwise swapping a model silently reuses stale vectors.
(Everything in REVIEW sec 5 — enumeration, M1/M2/control tagging, b003 forced-in, gold 102 rows, deterministic cache, fail-on-unlabeled, guards — was independently verified and stays.)

### REWORK-2b — CodeX instrumentation refinements (ACCEPTED, binding; they protect result honesty)
R1. **Preflight REAL model identity (fail-closed).** LM Studio may serve whatever is LOADED regardless of the requested model name. Before embedding: hit `/v1/models` (and/or embed one probe sentence), record `model_alias`, `endpoint_model`, `hf_repo`, `quant`, `embedding_dim`; **FAIL** if loaded != expected config. Cache key = `alias | endpoint_model | version/quant | prefix_profile | normalized_text`. (Verify-don't-trust applied to the instrument.)
R2. **Prefix = full per-model config table, not an E5 special-case:** `labse:{q:"",p:""}`, `bge-m3:{q:"",p:""}`, `e5:{q:"query: ",p:"passage: "}`. Future prefix changes = config edit, not code. Report records the prefixes used (prefix is part of the instrument and of the cache key).
R3. **`position_narrow` exact formula (pre-locked):** source occ in source sentence index `i`; `rel = i / max(1, n_source_sentences - 1)`; **target_index = round(rel * (n_target_sentences - 1))** (round, not floor — symmetric across start/mid/end). If `n_source_sentences == 1` or `n_target_sentences == 1`, tag region `degenerate_position_region` (region = whole block).
R4. **Report `reject` and `abstain` as SEPARATE outcomes** (reject = "confident: no accepted form in region"; abstain = "unsure"). Break every reference into: `assign_correct`, `assign_wrong`, `reject_correct`, `reject_wrong`, `abstain`. Do NOT collapse reject into abstain.
R5. **`position-window=0` is the PRIMARY pre-registered decision; window=1 is DIAGNOSTIC only** — may NOT be used to choose the model/method unless pre-registered. (Stops picking the post-hoc favourable window.)
R6. **Models may be unavailable — degrade gracefully, never fail the whole task.** If a model can't load, mark it `model_unavailable` / `skipped_with_reason` and still run `position_narrow` + any available model (at least LaBSE). BUT: the step-2/3 model-adoption verdict can only be reached if the models actually ran; if skipped, the report states "insufficient to choose a model" rather than defaulting to one.

## 1. Why
EV-08's block-scoped COUNT adherence credits a source occurrence when an accepted VN form is present in the block, capped by `min(source_count, active_target_count)`. Block-level counting cannot tell WHICH source occurrence a target token renders. Two distinct failure modes — EV-08 mitigates only one:

- **(M1) True collision** — one target form is an accepted form of >=2 ACTIVE registry terms in the block. EV-08 detects these (`collision_target_spans` = 884 in A_registry), excludes them from the lower bound, reports a BAND. EV-08 **abstains** here — the fix's job is to RESOLVE.
- **(M2) Variant stealing** — a single active term has MORE accepted-form target tokens than its source count, because a generic accepted variant in the block actually renders a DIFFERENT (often UNTRACKED) source word. EV-08 does NOT flag this and **wrongly credits it in the lower bound**. A FALSE POSITIVE surviving the honest headline.

**Canonical case `d2l_introduction_index_b003` (verified vs EV-08 audit):** registry has only `gl_user` (user->"người dùng", allowed=["người dùng","khách hàng"]); **no `customer` registry term**. Block: `customer`->"khách hàng" (x2, untracked), `user`->"người dùng" (x1). Audit shows "khách hàng" `role=active registry:user` — NOT a collision. Count `min(1,{người dùng:1,khách hàng:2}=3)=1` -> credited. This is **M2 variant-stealing**, OUTSIDE the 884 collision set.

**Why region-narrowing can fix M2 WITHOUT knowing `customer`:** it is whose-agnostic — it scopes to the source occurrence's corresponding target SENTENCE and asks "is an accepted form of THIS term here?". When `customer`'s "khách hàng" sits in a different sentence it falls OUTSIDE the region and is excluded — no need to identify it as `customer`'s. The residual that narrowing CANNOT fix is the **same-sentence stealer with an untracked source word** (both renderings in one sentence) — that needs a later LLM/word-align tier, OUT of scope here.

Open question: **does narrowing to the corresponding target sentence — by free POSITION or by an EMBEDDING model — (i) RESOLVE M1 collisions EV-08 abstains and (ii) avoid M2 false-positives EV-08 credits; and if a model helps, does it beat the FREE position baseline enough to justify its cost?**

Discipline (do NOT violate):
- `112/118` is region-recall on the EASY localization gold, NOT assignment-precision on the hard subset. Decide on the **ambiguous-assignment subset**, stratified single-/multi-sentence. (`scoring-scope-equals-production-scope`, `occurrence-weighted-block-anymatch-inflation`.)
- A model must EARN its cost vs the free `position_narrow`; "newer/higher-MTEB" is not evidence — the gold is. (`prompt-memory-design-is-first-class`, token economy.)
- Alignment fixes **mis-assignment only**, not Builder over-permissiveness (if real `user` was rendered "khách hàng" and Builder allows it, adherence still credits — only B-gold judges meaning). A_registry stays "Builder adherence".
- DEV result SELECTS the method; NOT a trust number (held-out is EV-07c). Lock method + thresholds + model choice A PRIORI. (`dont-tune-intervention-on-test-baseline`.)

## 2. Scope
**IN:**
1. **Ambiguous-assignment gold (unit = SOURCE OCCURRENCE)** — UNCHANGED from REVIEW build (102 rows: 57 true_collision / 25 variant_stealing / 20 control; b003 user forced in; columns per sec 3). Reuse as-is.
2. **HUMAN labeling** — UNCHANGED: annotator marks `gold_target_span` + `gold_label` (rendered/not_rendered/ambiguous) reading ONLY source + current-config target text; blind to registry, other config, any method prediction.
3. **FOUR reference points scored against the SAME gold:**
   - `legacy_block_count` — pre-EV-08 block any-match credit (picks first in-block candidate). What M2 fails on.
   - `abstain_baseline` — EV-08 lower-bound (abstains M1; still credits M2). Both above are position-blind `candidates[0]` proxies of the COUNT metric — kept intentionally to model the metric's blindness.
   - `position_narrow` (**NEW, model-free**) — narrow to the target sentence at the source occurrence's MONOTONIC relative position (rel_pos = source-sentence-index / n_source_sentences, mapped to target-sentence-index; region = that sentence, configurable +/-1), then the existing deterministic accepted-form match WITHIN the region; **abstain** if >1 survives, **reject (not-credited)** if 0 survive. The cheap floor an embedding model must beat.
   - `region_narrow@<model>` (the candidate fix) — same as position_narrow but the region = UNION of top-k=3 target sentences ranked by EMBEDDING cosine; run once per model in `{LaBSE, BGE-M3, multilingual-e5-large}`.
4. **Per-reference metrics on the subset, split by probe_type**: assignment-precision (non-abstained correctly assigned vs gold), coverage = 1-abstain-rate, abstain-rate, wall-time. Decision breakdowns:
   - M1: of collisions `abstain_baseline` abstains, how many does each narrowing reference RESOLVE / abstain / get wrong.
   - M2: of variant-stealing `abstain_baseline` wrongly credits, how many does each correctly NOT-credit (assign-elsewhere / reject / abstain) vs still wrongly credit.
   - control: narrowing must introduce NO new errors vs `abstain_baseline`.
   - **model vs free**: region_narrow@model assignment-precision MINUS position_narrow, on the hard subset — the added value of the model.
5. **Stratification** — UNCHANGED: fraction of source occurrences (and M1 884 / M2 populations) in multi- vs single-sentence blocks.
6. **Embeddings via LM Studio `/v1/embeddings` (GPU OK)**, vectors **frozen to disk cache** keyed by (REAL model_id+version, normalized-text hash); re-run reads cache -> identical numbers. Record each model_id+version, cache hit/miss, wall-time. E5 uses `query:`/`passage:` prefixes (configured per model; prefixes are part of the cached text so the cache stays correct).

**OUT (do not wander):**
- NO change to the headline metric / `d2l_translate_score.py`. Probe writes `data/reports/collision_assignment_probe.json` only.
- NO clause/hybrid-splitter arms. Only top-k sentence UNION (embedding) or single-sentence+/-1 (position) + deterministic disambiguation.
- NO awesome-align / SimAlign / mBERT word-alignment; NO LLM tier (the same-sentence residual is a later task).
- NO re-translate; frozen DB unchanged. NOT a trust number.

## 3. Spec
Files (eval-only; modules graduate to headline ONLY if sec 6 adopts a narrowing reference):
- **`pipeline/eval/region_align.py`** (exists; extend): keep cache + cosine top-k. CHANGES: (a) cache key + report bind the REAL model identity (pass `--model` and `--model-version` = HF repo+quant, e.g. `gpustack/bge-m3-GGUF:Q8_0`), drop the constant placeholder; (b) per-model query/passage PREFIX config (`""` for LaBSE/BGE-M3, `"query: "`/`"passage: "` for E5); prefixes are prepended BEFORE caching.
- **`pipeline/eval/ambiguous_assignment.py`** (exists; extend): add `position_narrow` decision (monotonic relative-position region; reuse `split_sentences`/`containing_unit`/`span_in_ranges`); add `reject` decision status (0 in-region candidate = confidently not-credited, distinct from `abstain`); loop `region_narrow` over the model list; `_decision_correct`: gold `not_rendered` correct iff status in {abstain, reject}; gold `rendered` correct iff status==assign and span matches; gold `ambiguous` correct iff status==abstain.
- **`pipeline/scripts/eval_ambiguous_assignment.py`** (exists; extend): accept `--models "LaBSE=text-embedding-labse@v1,bge-m3=bge-m3@Q8_0,e5=multilingual-e5-large@Q8_0"` (name=endpoint-model@version, with prefix profile) and `--position-window N` (default 0 => single sentence, 1 => +/-1). Report: all four+ references' metrics by probe_type, M1/M2 breakdowns, model-minus-position deltas, stratification, per-model id+version+cache+wall-time, k=1,2,3 sensitivity, frozen-DB hash, scope statement.
- **`build_ambiguous_assignment_gold.py`** + `collision_assignment_gold.csv` + `.gitignore embed_cache` — UNCHANGED.
- Sentence segmentation unchanged (record EN/VI splitter). Within-region match reuses `surface_match.allocate_spans`/`find_spans` + EV-08 matcher; only SEARCH SCOPE differs across references.
- **Pre-locked params (a priori):** k=3; report k=1,2,3 but DECIDE on k=3. `position-window`=0 (single sentence) primary; report window=1 as sensitivity. region/position abstain when >1 survives, reject when 0 survive.

**Decision rule (fixed BEFORE running; applied in sec 6).** In ORDER:
1. **Does narrowing help at all?** `position_narrow` must, on the hard subset, (a) avoid >=50% of M2 false credits `abstain_baseline` makes, (b) resolve >=50% of M1 collisions `abstain_baseline` abstains, (c) add NO new control errors. If it fails all, keep EV-08; narrowing rejected.
2. **Is a MODEL worth its cost?** Adopt an embedding model ONLY IF the best `region_narrow@model` beats `position_narrow` on hard-subset assignment-precision by a MEANINGFUL margin (pre-register: >= 10 percentage points OR resolves a class position cannot — reordered/merged sentences) at acceptable wall-time. Otherwise ship `position_narrow` (free) as the narrowing layer.
3. **Which model?** Among models clearing step 2, pick the highest hard-subset assignment-precision; ties -> lower cost. Report the choice + the margin; do NOT pick by reputation.
All bars pre-registered, not fitted. If the subset is too small to separate references, report THAT as the finding rather than forcing a verdict. Gain must concentrate in multi-sentence blocks (mechanism check).

## 4. Acceptance criteria
```bash
# 1. Gold stub — already generated/labeled; regeneration is deterministic (seed=42):
python -m pipeline.scripts.build_ambiguous_assignment_gold \
  --report data/reports/d2l_translation_metrics_v2.json \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --n 80 --n-control 20 --out data/eval/collision_assignment_gold.csv

# 2. [HUMAN STEP] annotator fills gold_target_span + gold_label for every row.

# 3. Run the probe with ALL references incl. position_narrow + the 3 models
#    (LM Studio: load each embedding model; Q8_0/f16):
python -m pipeline.scripts.eval_ambiguous_assignment \
  --gold data/eval/collision_assignment_gold.csv \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --k 3 --position-window 0 \
  --embed-endpoint http://localhost:1234/v1/embeddings \
  --models "labse=text-embedding-labse@labse-q8,bge-m3=bge-m3@Q8_0,e5=multilingual-e5-large@Q8_0" \
  --out data/reports/collision_assignment_probe.json
#   -> FAILS if any gold row unlabeled.
#   -> prints, by probe_type, for {legacy_block_count, abstain_baseline, position_narrow,
#      region_narrow@labse, region_narrow@bge-m3, region_narrow@e5}:
#      assignment-precision / abstain-rate / wall-time; M1-resolved + M2-avoided;
#      model-minus-position delta; per-model id+version+cache stats.

# 4. Determinism (warm cache): re-run step 3 -> identical numbers, cache hit-rate 100% per model.

# 5. Guards:
#    - frozen DB SHA-256 first16 == DA0F687894090D43 (printed, unchanged).
#    - git diff data/reports/d2l_translation_metrics_v2.json -> EMPTY.
#    - grep -q 'data/eval/embed_cache/' .gitignore -> present.

# 6. Tests:
python -m pytest pipeline/tests -k "region_align or ambiguous_assignment" -v
#   add: position_narrow assigns/abstains/rejects correctly; reject vs abstain status;
#        per-model prefix applied + cache keyed by real model id; E5 prefix present.
python -m pytest pipeline/tests app/backend/tests   # full suite green
```

Report must let sec-6 APPLY the ordered decision rule without re-running: four+ references' metrics by probe_type, M1/M2 breakdowns, model-minus-position deltas, multi-sentence stratification, k & window sensitivity, per-model id+version+cache+wall-time, frozen-DB hash, scope statement.

## 5. Implementation notes *(CodeX fills — REVIEW build below is verified; append REWORK-2 additions)*

Implemented eval-only machinery, no headline/scorer mutation and no re-translation.

Files changed/added:
- Added `pipeline/eval/region_align.py`: LM Studio `/v1/embeddings` client, disk cache keyed by `model_id|model_version|normalized_text`, sentence splitting, cosine top-k target sentence ranking, union-range helpers.
- Added `pipeline/eval/ambiguous_assignment.py`: shared EV-09 data model, ambiguous-assignment row enumeration, stratified sampling, CSV stub writer, labeled-gold loader, three reference evaluators (`legacy_block_count`, `abstain_baseline`, `region_narrow`), JSON/HTML report writer.
- Added CLI scripts:
  - `pipeline/scripts/build_ambiguous_assignment_gold.py`
  - `pipeline/scripts/eval_ambiguous_assignment.py`
- Added tests:
  - `pipeline/tests/test_region_align.py`
  - `pipeline/tests/test_ambiguous_assignment.py`
- Added `.gitignore` entry: `THESIS_RUNTIME_TOOL/data/eval/embed_cache/`.

Important implementation detail:
- The canonical `d2l_introduction_index_b003` `user` variant-stealing case is forced into the sampled CSV if present, because it is the rationale guard for the reworked task. It is not used to tune metrics; it verifies the population builder catches M2. Both S0 and S1 rows are included, so the generated stub has 102 rows rather than exactly 100.
- Gold rows are source-occurrence based. Candidate target spans are serialized in `candidate_target_spans` JSON, and `source_text`/`target_text` are included even though not listed in the minimal spec, because the human annotator must see the current-config texts to label spans.
- `eval_ambiguous_assignment` intentionally hard-fails before any embedding request if any `gold_label` is empty.

Generated artifact:
```text
data/eval/collision_assignment_gold.csv
```

Build command/output:
```bash
python -m pipeline.scripts.build_ambiguous_assignment_gold \
  --report data/reports/d2l_translation_metrics_v2.json \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --n 80 --n-control 20 --out data/eval/collision_assignment_gold.csv
```

```json
{
  "rows": 102,
  "ev08_a_registry": {
    "denominator": 8149,
    "confirmed": 6628,
    "residual_capacity": 856,
    "collision_target_spans": 884
  },
  "population": {
    "variant_stealing": 852,
    "control": 392,
    "true_collision": 2171
  },
  "selected": {
    "control": 20,
    "true_collision": 57,
    "variant_stealing": 25
  },
  "stratification": {
    "single_multi": {
      "control:multi": 18,
      "control:single": 374,
      "true_collision:multi": 2117,
      "true_collision:single": 54,
      "variant_stealing:multi": 824,
      "variant_stealing:single": 28
    }
  }
}
```

Guard checks:
```bash
python -m pipeline.scripts.eval_ambiguous_assignment \
  --gold data/eval/collision_assignment_gold.csv \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --k 3 --embed-endpoint http://localhost:1234/v1/embeddings \
  --out data/reports/collision_assignment_probe.json
```

Expected current result: fails before embedding because all generated gold rows are unlabeled:
```text
ValueError: Gold rows are not fully labeled: [...]
```

This is intentional and satisfies the human-label gate. No `collision_assignment_probe.json` was produced.

Verification:
```bash
python -m py_compile pipeline/eval/region_align.py pipeline/eval/ambiguous_assignment.py pipeline/scripts/build_ambiguous_assignment_gold.py pipeline/scripts/eval_ambiguous_assignment.py
git diff -- data/reports/d2l_translation_metrics_v2.json
Select-String -LiteralPath '.gitignore' -Pattern 'data/eval/embed_cache/' -SimpleMatch
python -m pytest pipeline/tests -k "region_align or ambiguous_assignment" -v
python -m pytest pipeline/tests app/backend/tests
```

Results:
- `d2l_translation_metrics_v2.json` unchanged.
- `.gitignore` contains `THESIS_RUNTIME_TOOL/data/eval/embed_cache/`.
- Targeted tests: `6 passed`.
- Full suite: `338 passed in 122.26s`.

### REWORK-2 additions *(CodeX fills)*

Implemented REWORK-2/2b instrumentation without changing the frozen scorer/headline.

Code changes:
- `pipeline/eval/region_align.py`
  - Added `EmbeddingModelConfig` / `EmbeddingModelIdentity`.
  - Added config table:
    - `labse`: `text-embedding-labse`, no prefixes.
    - `bge-m3`: `text-embedding-bge-m3`, no prefixes.
    - `e5`: `text-embedding-multilingual-e5-large-instruct`, `query: ` / `passage: ` prefixes.
  - Added LM Studio preflight through `/v1/models`, native `/api/v1/models`, and one embedding probe.
  - Cache key now includes `alias | endpoint_model | model_version/quant/dim | prefix_profile | normalized_text`.
  - Embedding response model mismatch now raises instead of silently accepting wrong vectors.
- `pipeline/eval/ambiguous_assignment.py`
  - Added primary `position_narrow` with pre-locked formula:
    `target_index = round((source_sentence_index / max(1, n_source_sentences - 1)) * (n_target_sentences - 1))`.
  - Added diagnostic `position_narrow_w1`.
  - Added `reject` status for 0 in-region candidate, distinct from `abstain` for >1 survivor.
  - Runs `region_narrow@<alias>` for every available model and records skipped models instead of failing the whole task.
  - Report now includes model identities, prefix profile, cache stats, `assign_correct/assign_wrong/reject_correct/reject_wrong/abstain`, model-minus-position deltas, and k/window sensitivity.
- `pipeline/scripts/eval_ambiguous_assignment.py`
  - Added `--models` and `--position-window`; kept `--embed-model` as deprecated compatibility.

Local LM Studio preflight (no gold scoring; only model availability/identity):
```json
[
  {"alias":"labse","endpoint_model":"text-embedding-labse","status":"available","hf_repo":"ChristianAzinn/text-embedding-labse","quant":"Q8_0","dim":768,"query_prefix":"","passage_prefix":""},
  {"alias":"bge-m3","endpoint_model":"text-embedding-bge-m3","status":"available","hf_repo":"gpustack/text-embedding-bge-m3","quant":"Q8_0","dim":1024,"query_prefix":"","passage_prefix":""},
  {"alias":"e5","endpoint_model":"text-embedding-multilingual-e5-large-instruct","status":"available","hf_repo":"Ralriki/text-embedding-multilingual-e5-large-instruct","quant":"Q8_0","dim":1024,"query_prefix":"query: ","passage_prefix":"passage: "}
]
```

Production CLI gate:
```bash
python -m pipeline.scripts.eval_ambiguous_assignment \
  --gold data/eval/collision_assignment_gold.csv \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --k 3 --position-window 0 \
  --embed-endpoint http://127.0.0.1:1234/v1/embeddings \
  --models "labse=text-embedding-labse@ChristianAzinn/labse-gguf:Q8_0,bge-m3=text-embedding-bge-m3@gpustack/bge-m3-GGUF:Q8_0,e5=text-embedding-multilingual-e5-large-instruct@Ralriki/multilingual-e5-large-instruct-GGUF:Q8_0" \
  --out data/reports/collision_assignment_probe.json
```
Result: intentional fail before any embedding because current `collision_assignment_gold.csv` has 102/102 unlabeled rows:
```text
ValueError: Gold rows are not fully labeled: [...]
```
No `data/reports/collision_assignment_probe.json` or `.html` was produced.

Verification:
```bash
python -m py_compile pipeline/eval/region_align.py pipeline/eval/ambiguous_assignment.py pipeline/scripts/eval_ambiguous_assignment.py
python -m pytest pipeline/tests -k "region_align or ambiguous_assignment" -v
python -m pytest pipeline/tests app/backend/tests
git diff -- THESIS_RUNTIME_TOOL/data/reports/d2l_translation_metrics_v2.json
```

Results:
- Targeted tests: `11 passed`.
- Full suite: `343 passed in 124.44s`.
- `d2l_translation_metrics_v2.json` unchanged.
- `.gitignore` already contains `THESIS_RUNTIME_TOOL/data/eval/embed_cache/`.
- Frozen DB was not written; no translation/scorer headline was regenerated.

## 6. Review (Claude) — VERDICT: PASS (machinery; NO trust number yet — gold unlabeled by design)

Reviewed REWORK-2b by reading the on-disk source (not the §5 report), then re-ran tests + guards myself.

**R1 model identity (PASS):** `preflight_embedding_model` hits `/v1/models` + an embed probe, returns `model_unavailable` on mismatch/not-loaded; `EmbeddingCacheClient.embed` ALSO raises if the response `model` != requested. Cache key = `alias | endpoint_model | model_version | prefix_profile | text` (`region_align.cache_key`), with `model_version = cache_model_version()` folding `hf|quant|dim`; cache dir is per-alias. No cross-model collision possible.
**R2 prefix config (PASS):** `DEFAULT_MODEL_CONFIGS` is a table — labse/bge-m3 empty, e5 `query: `/`passage: `; `top_k_target_sentences` prepends `query_prefix`/`passage_prefix` BEFORE embedding, so the prefix is part of both the cached text and the key. Report records the prefixes.
**R3 position formula (PASS, test-locked):** `_position_ranges` → `rel = source_index / max(1, n_source-1)`, `target_index = round(rel * (n_target-1))`, and `len<=1` either side → `degenerate_position_region` (whole block). Locked by `test_position_narrow_uses_relative_sentence_index` (rel=1 → target sentence 2 → assigns "nguoi dung").
**R4 reject≠abstain (PASS, test-locked):** 0 in-region survivors → `reject`, >1 → `abstain`, 1 → `assign`; `_decision_correct` counts gold `not_rendered` correct iff status ∈ {abstain, reject}; `_metric_summary` emits `assign_correct/assign_wrong/reject_correct/reject_wrong/abstain` separately (+ `non_abstain_accuracy`). Locked by `test_position_narrow_rejects_when_no_candidate_survives_region`.
**R5 window (PASS):** primary `position-window=0`; diagnostic `position_narrow_w1` and `:k1/:k2` are excluded from `_primary_references`, so they cannot drive the decision.
**R6 graceful (PASS):** only `available` identities get clients; skipped models are recorded with reason; the model-adoption verdict can only be reached over models that actually ran.

**Guards re-derived by me (not trusting §5):**
- targeted EV-09 tests **11/11**; full `pipeline/tests app/backend/tests` **343/343** (re-ran, 106s).
- frozen DB SHA-256 first16 = **DA0F687894090D43** unchanged; `data/reports/d2l_translation_metrics_v2.json` **no diff**.
- `collision_assignment_probe.json` **absent**; `collision_assignment_gold.csv` = **102 rows / 0 labeled** (correct — human gate holds).
- enumeration/tagging from the REVIEW build re-verified earlier: b003 `user` = `variant_stealing`/`active_surplus`, candidates {khách hàng, người dùng}.

**Minor (non-blocking, note for when real numbers run):** preflight `hf_repo` is derived from the LM Studio native key (shows e.g. `ChristianAzinn/text-embedding-labse`, not the true GGUF repo `ChristianAzinn/labse-gguf`). Cosmetic only — the authoritative version string lives in `model_version` and the per-alias key already disambiguates; clean up the label before publishing the probe table.

**Status:** machinery PASS, committed. The task stays open for DATA: human labels 102 gold rows → run the probe → a follow-up review applies the §3 ORDERED decision rule (does narrowing help → is a model worth >position by ≥10pp → which model) on the real numbers. No trust number is claimed here.

## 7. DATA results (human-labeled gold; DEV, NOT a trust number)
Gold fully labeled by the user (102/102, verified: rendered span correct incl. ~10 NON-candidate spans found via select — a real candidate-coverage gap in the EV-08 allocator). Probe run with all 3 models loaded in LM Studio (labse 768, bge-m3 1024, e5 1024 w/ query:/passage:), k=3, window=0, elapsed 62.5s, frozen DB DA0F687894090D43 unchanged.

### 7.1 Decision-rule outcome (hard subset = true_collision + variant_stealing)
Assignment-precision: **position_narrow 0.765**, bge-m3 0.545 (−0.22), labse 0.52 (−0.24), e5 0.50 (−0.26). Control: all 1.0.
- **M2 variant_stealing (25):** legacy & abstain_baseline prec 0.16 (4 correct, **21 wrong**) → confirms M2 false-positives SURVIVE EV-08's lower bound. position_narrow prec **1.0** (7 correct, 0 wrong, 18 abstain) → converts all 21 false-credits to honest abstentions.
- **M1 true_collision (57):** abstain_baseline abstains all → position_narrow 19 correct / 8 wrong / prec 0.70 / cov 0.58 (resolves 33%, below the 50% bar).
- **Verdict:** Step-2 FAILS for every embedding model (all lose to FREE position by 22–26 pp). **Embedding models REJECTED for the headline/scoring layer.** position_narrow adopted as the assignment layer: fixes M2 by abstaining (tightens lower bound honestly), resolves confident M1, abstains the rest into the EV-08 band. My prior "LaBSE wins multi-sentence" hypothesis was REFUTED by data — the free position baseline existed precisely to catch this.

### 7.2 BUT EV-09 measures span-assignment+abstain, NOT sentence-matching
The user's actual tier-1 goal is sentence↔sentence region-narrowing (to feed a tier-2 LLM). Re-measured **sentence-hit recall** (does the gold's VI sentence fall in the model's top-k?) on the 60 multi-sentence rows:
- **top-1:** bge-m3 0.983, e5 0.983, labse 0.950, position 0.967. **top-3: ALL 1.000.**
- So models are NOT bad at sentence matching (~98% top-1); the EV-09 "loss" was the k=3 union width + within-sentence disambiguation (tier-2 work) + abstain accounting. position ties the models **only because D2L preserves sentence order** (a corpus-specific crutch that will break on reordered/summarized literary text — UNMEASURED, argued).

### 7.3 Handling top-1 < 100% (margin + rank analysis)
- **Every top-1 miss had the gold at rank 2** (gold_rank=1) across all models → **top-2 catches 100% of misses** on this sample (no need for top-3). Escalate top-2, not top-3.
- **Margin (cos#1−cos#2) as a confidence gate separates only for bge-m3:** its single fail sits below pass-q1 (0.136 vs q1 0.212) → a gate escalates ~7% to catch it. e5 margins are compressed (gate → 32% escalation); labse has a high-margin fail (0.145 > median) → margin can't flag it. → another reason **bge-m3 is the tier-1 model of choice**.
- The single hard case is the SAME occurrence across models (`b023 attributes:0`, VI near-duplicate attributes/features sentences) — intrinsically ambiguous, gold at rank 2.

### 7.4 Recommendations carried forward (NOT yet built)
1. **Scoring/assignment layer:** adopt deterministic `position_narrow` (free) + abstain→band; do NOT add an embedding model to the SCORER. (D2L; integration = a new task.)
2. **Tier-1 region-narrower for the tier-2 LLM:** use **bge-m3 top-1**; on low margin escalate **top-2** to the LLM. Drop position from production (helps in no regime: redundant when order-preserved, misleading when not) and drop LaBSE (weakest). Pass cases yield the EN-sentence↔VI-sentence 1:1 pair for tier-2.
3. **Caveats (loud):** DEV not trust number; n=60 multi-sentence / n=1 bge-m3 fail = anecdote — DO NOT lock the margin threshold here. Validate on a larger held-out incl. reordered/literary text (where position should break and the model's semantic robustness can actually be demonstrated). Candidate-coverage gap (~10 non-candidate gold spans) is an allocator limitation, separate from narrowing, for future work.
