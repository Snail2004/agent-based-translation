# TASK_EV_D2L_03_term_policy — Phân tầng constraint_strength (a-priori) + dọn glossary ở tầng EVAL-overlay + headline D_surface_v2 = hard-only; 0 API re-score, KHÔNG re-translate

- **Status:** DONE / PASS (Claude, 2026-06-17)
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (pp) Cấp 2, (qq) · memory `d2l-scorer-validity-and-remediation-ladder`, `dont-tune-intervention-on-test-baseline` · EV_D2L_02 (commit 47620f4)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

EV_D2L_02 đã diệt nesting/URL/case ở scorer (D_surface_v2). Residual drift (S1 184) = **(a) generic-form cross-occurrence** (vd "chính xác" tính từ ở block khác) + **(b) cross-term leakage** (vd `calculus` báo drift vì `phép tính` — biến thể Builder gán SAI, thực ra là bản dịch của `calculations`). (pp) chốt Cấp 2 = **phân tầng term + dọn glossary** để headline D chỉ chấm term **đáng-bất-biến**.

**RÀNG BUỘC CỨNG:** `glossary_entries`/`entities` đang **FROZEN** (FREEZE triggers insert/update/delete — đã xác minh trên DB thật). ⇒ cleanup **KHÔNG được ghi DB**; phải làm ở **tầng EVAL-overlay (CSV eval-only, scorer áp khi chấm)**. Vừa tôn trọng Directional-Lock (memory agent-xây giữ nguyên) vừa đúng "đo NGOÀI model".

**Mục tiêu EV_D2L_03 (0 API, re-score `translation_runs` cũ, KHÔNG re-translate):** (1) gán `constraint_strength` cho từng term bằng classifier **a-priori** (source-side + registry metadata + stoplist curated + override), (2) dọn allowed_variants pollution ở eval-overlay, (3) D_surface_v2 **headline = chỉ `hard`** + báo cáo per-tier, (4) re-score → calculus consistent, AI/generic ra khỏi headline. **Re-translate là task SAU, theo từng bậc nhỏ.**

## 2. Scope

- **IN (0 API):**
  1. `constraint_strength ∈ {hard, soft, preserve, entity, ignore_for_consistency}` — classifier **a-priori deterministic** (KHÔNG nhìn output S0/S1).
  2. **Eval-overlay glossary cleanup** (CSV eval-only): bỏ allowed_variant pollution (vd calculus⊅"phép tính"), canonical-override (vd rules→"quy tắc") — scorer áp khi load, **KHÔNG ghi `glossary_entries`**.
  3. Scorer: headline `D_surface_v2` = **chỉ term `hard`**; thêm `by_tier` breakdown (đếm theo từng strength) + gắn `constraint_strength` vào `terms_all`; giữ no-recompute ở app.
  4. Re-score existing `translation_runs` (0 API, kèm `--gold-variants`) → report cập nhật; in delta headline-all vs headline-hard.

- **OUT (CẤM):**
  - **RE-TRANSLATE bất kỳ** — full 4 chương để SAU; **test nhỏ trước** = `EV_D2L_04` pilot **1 chương** (cost-gated, preflight prompt review per (ll)/(pp)). CẤM API trong task này.
  - Đổi injection prompt S1 (chỉ CHUẨN BỊ policy; hiệu lực injection test ở pilot re-translate).
  - Alignment (`ALIGN_PILOT_01`).
  - Ghi DB / sửa frozen memory.
  - **Chọn tier hay justify cleanup bằng drift trên benchmark** (tuning-on-test — memory `dont-tune-intervention-on-test-baseline`). Mọi tier/fix phải có lý do **linguistic/a-priori**, KHÔNG phải "vì nó drift".

## 3. Spec *(Claude viết)*

**`pipeline/eval/term_policy.py` (MỚI):** `classify_term(row, *, stoplist, hard_allowlist, overrides) -> str` — thuần, testable, a-priori:
- `overrides[source_term]` thắng tuyệt đối (chỉnh tay linguistic).
- `preserve` nếu `do_not_translate=1` hoặc `term_type=="code_api"`.
- `entity` nếu `term_type=="proper_noun"` (D2L entities table rỗng → proper noun nằm ở glossary).
- `abbreviation` → `soft` (thường giữ acronym EN; vd AI).
- còn lại (`term`): multi-word (≥2 token) → `hard`; single-word → (`∈ stoplist` → `ignore_for_consistency`; `∈ hard_allowlist` → `hard`; else `soft`).

**Files eval-only `data/eval/` (a-priori, có justification):**
- `d2l_term_stoplist.txt` — single-word phổ thông/đa nghĩa (vd rule, class, set, example, value, average, degree, domain…). Biên a-priori theo tính-từ-vựng, KHÔNG theo drift.
- `d2l_term_hard_allowlist.txt` — single-word đơn-nghĩa kỹ thuật (vd tensor, gradient, softmax, backpropagation…).
- `d2l_term_policy_overrides.csv` — `source_term,constraint_strength,justification`.
- `d2l_glossary_fixes.csv` — `source_term,op,value,justification` với `op ∈ {remove_variant, set_canonical}` (vd `calculus,remove_variant,phép tính,"phép tính = calculation, sai nghĩa cho calculus trong văn bản này"`).

**`pipeline/eval/d2l_translate_score.py`:**
- `_load_registry_rows`: sau khi load, **áp `d2l_glossary_fixes.csv`** (remove_variant/set_canonical) lên bản sao in-memory — KHÔNG ghi DB; gắn `constraint_strength` qua `classify_term`.
- `_score_registry_consistency`: vẫn chấm mọi tier nhưng **headline (`overall`) = chỉ `hard`**; thêm `by_tier={strength: {terms,consistent,drift,undetected,overall}}`; mỗi `terms_all[i]` thêm `constraint_strength`. Giữ `method/alignment/headline_ready` (false).
- Limitations: thêm dòng "headline = hard-tier only; soft/ignore/preserve/entity reported for transparency, not in headline".

**`app/backend/services/thesis_scores.py`:** headline D đọc `overall` (hard-only) + nhãn `D_surface_v2 (hard-tier)`; `_d2l_drift`/overlay đọc `constraint_strength` từ `terms_all` (overlay có thể nhạt term không-hard). no-recompute.

**Ràng buộc (LOCK):** observe⊥compute (nn).1; **freeze: KHÔNG ghi glossary_entries/entities** (eval-overlay only); tuning-on-test guard; 0 API.

## 4. Acceptance criteria *(lệnh chạy được)*

```bash
cd THESIS_RUNTIME_TOOL

# (1) Classifier a-priori + eval-overlay
python -m pytest pipeline/tests/test_term_policy.py pipeline/tests/test_d2l_translate_score.py -v   # PASS, gồm:
#   test_classify_buckets        : do_not_translate→preserve, proper_noun→entity, multiword→hard, stopword→ignore, allowlist→hard
#   test_eval_overlay_no_db_write: áp fixes KHÔNG ghi glossary_entries (frozen) — đếm row/hash trước-sau bằng nhau
#   test_calculus_consistent_after_fix : bỏ "phép tính" khỏi candidate → calculus status==consistent

# (2) Re-score 0 API (KHÔNG translate) — KÈM --gold-variants (parity với v1/v2)
python -m pipeline.scripts.score_run \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --chapters d2l_introduction d2l_preliminaries d2l_linear_networks d2l_multilayer_perceptrons \
  --config S1 --gold-variants data/eval/d2l_gold_variants.csv \
  --out data/reports/d2l_translation_metrics_v2.json
# → D_registry_consistency.S1.overall = headline HARD-only; by_tier có mặt;
#   calculus status=consistent; AI tier∈{soft,preserve} (ngoài headline)

# (3) Frozen DB bất biến: assert score chạy không trigger FREEZE (không ghi)
python -m pytest pipeline/tests app/backend/tests -q   # full xanh, không regression
```

## 5. Implementation notes *(CodeX điền)*

Implemented EV-D2L-03 as a 0-API scorer/policy change only. No re-translation and no writes to the frozen runtime memory tables.

### Code changes

- Added `pipeline/eval/term_policy.py`
  - `classify_term(row, *, stoplist, hard_allowlist, overrides)` implements the a-priori deterministic buckets:
    `hard`, `soft`, `preserve`, `entity`, `ignore_for_consistency`.
  - `load_term_policy_assets(root)` loads eval-only policy/fix files.
  - `apply_glossary_fixes(...)` applies `remove_variant` / `set_canonical` to row copies only.
- Added eval-only policy files under `data/eval/`
  - `d2l_term_stoplist.txt`
  - `d2l_term_hard_allowlist.txt`
  - `d2l_term_policy_overrides.csv`
  - `d2l_glossary_fixes.csv`
- Updated `pipeline/eval/d2l_translate_score.py`
  - Loads eval policy files from `data/eval` by default.
  - Keeps raw registry rows for A/injection diagnostics.
  - Uses eval-overlay registry rows for D only, so `calculus` cleanup does not rewrite what Builder originally produced.
  - `D_registry_consistency[config].overall` is now the hard-tier headline.
  - Adds `headline_tier`, `all_terms`, `by_tier`, and `constraint_strength` in `terms_all`.
  - Adds `term_policy` provenance in the report.
- Updated app read-model/overlay
  - D headline label is now `D_surface_v2 (hard-tier)`.
  - `constraint_strength` is copied from report details into drift/overlay outputs.
  - UI hover card shows the tier for runtime term spans.

### Eval-overlay files

Current fixes are intentionally minimal and linguistic/a-priori:

```csv
calculus,remove_variant,phép tính,"phép tính is the natural Vietnamese rendering of calculation/calculations, not calculus as a mathematical field."
rules,set_canonical,quy tắc,"In mathematical and technical Vietnamese, rules is normally rendered as quy tắc rather than luật."
```

Current policy counts over the full frozen registry:

```text
entity: 13
hard: 1001
ignore_for_consistency: 13
preserve: 224
soft: 357
```

### Production re-score

Command:

```bash
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 --chapters d2l_introduction d2l_preliminaries d2l_linear_networks d2l_multilayer_perceptrons --config S1 --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_translation_metrics_v2.json
```

Output:

```text
S0: B flat=0.7541 (2322 pairs), B recurring=0.7540 (2285 pairs), D=0.7126 (414 terms)
S1: B flat=0.8191 (2322 pairs), B recurring=0.8214 (2285 pairs), D=0.8406 (414 terms)
S1 A registry TAR=0.9524 (8370 pairs)
```

Delta vs `data/reports/d2l_translation_metrics.json` (v1 report):

| Metric | S0 v1 | S0 EV-D2L-03 | S1 v1 | S1 EV-D2L-03 |
|---|---:|---:|---:|---:|
| B flat overall | 0.751931 | 0.754091 | 0.817167 | 0.819121 |
| B flat occurrence-weighted | 0.763867 | 0.766039 | 0.832013 | 0.834344 |
| D headline overall | 0.593007 | 0.712560 | 0.700699 | 0.840580 |
| D headline terms | 715 | 414 | 715 | 414 |
| D headline drift terms | 264 | 102 | 213 | 65 |
| D headline undetected terms | 27 | 17 | 1 | 1 |

S1 `by_tier`:

```text
hard: terms=414 consistent=348 drift=65 undetected=1 overall=0.840580
soft: terms=287 consistent=179 drift=107 undetected=1 overall=0.623693
preserve: terms=68 consistent=54 drift=7 undetected=7 overall=0.794118
entity: terms=2 consistent=2 drift=0 undetected=0 overall=1.000000
ignore_for_consistency: terms=13 consistent=2 drift=11 undetected=0 overall=0.153846
```

Spot checks:

- `calculus`: S0 and S1 are now `consistent`, hard tier, forms_used `{"giải tích": 7}`.
- `AI`: remains drift diagnostically but is `soft`, outside the hard headline.
- `rules`: canonical eval-overlay is `quy tắc`, but the term is `ignore_for_consistency`; S1 still drifts diagnostically and is outside the hard headline.

Frozen DB guard:

```text
glossary_entries hash before: 58e7797059ec3adbf041cd528efa9a671d3af00c60cf9ac3c0485d07eb5bacdf
glossary_entries hash after:  58e7797059ec3adbf041cd528efa9a671d3af00c60cf9ac3c0485d07eb5bacdf
```

### Verification

```text
python -m pytest pipeline/tests/test_term_policy.py pipeline/tests/test_d2l_translate_score.py -q
13 passed in 6.90s

python -m pytest app/backend/tests/test_thesis_scores.py app/backend/tests/test_thesis_overlay.py -q
13 passed in 1.21s

python -m pytest pipeline/tests app/backend/tests -q
261 passed in 151.79s (0:02:31)
```

Pytest still emits the Windows `D:\temp\pytest-of-Snail\pytest-current` cleanup `PermissionError` after completion. The test result itself is green.

## 6. Review *(Claude điền)*

- **Verdict:** **PASS.** Tái kiểm độc lập trên DB thật (KHÔNG tin §5).

1. **Classifier a-priori (kiểm bằng code):** `classify_term` nhận registry row + 3 list, **không hề đọc output S0/S1** → đúng tinh thần không-tuning-on-test. Eval-overlay áp trên **bản sao** (`apply_glossary_fixes` copies); unit test `test_apply_glossary_fixes_on_copy_only` assert row gốc bất biến.
2. **Frozen DB bất biến:** tự hash `glossary_entries` trước/sau khi chạy scorer → **giống hệt** (`f68d3f46…`). A dùng `registry_rows` RAW (TAR 0.952 không đổi); B dùng gold (occ_w 0.834 = parity EV_D2L_02). Overlay chỉ chạm D.
3. **Determinism:** tự re-run `score_run` (kèm `--gold-variants`) → khớp committed (S1 `0.84058`).
4. **Headline hard-tier hợp lệ, KHÔNG gaming:** S0 **0.713** → S1 **0.841** trên **414 hard term** (không phải nhúm nhỏ); toàn registry hard=**1001/1608** (đa số). Bằng chứng anti-gaming: `derivatives` (hard, **drift** `{đạo hàm:20, vi phân:7}`) **vẫn nằm trong headline** dù trôi — không loại term bất lợi để làm đẹp số. `by_tier` minh bạch: `ignore` (set/class/rules, 0.154) + `soft` (0.624) = từ đa-nghĩa thật, đúng nằm NGOÀI headline.
5. **Lists linguistic, không cherry-pick:** stoplist 23 từ phổ thông (set/class/value/function/model…), allowlist 10 thuật ngữ đơn-nghĩa (tensor/gradient/softmax…), 1 override (AI→soft), 2 fix (calculus⊅phép tính, rules→quy tắc) — mỗi entry có justification ngôn ngữ **tổng-quát-hóa được** (đúng trên D2L bất kỳ, không chỉ benchmark này).
6. **Spot checks:** `calculus` hard **consistent** `{giải tích:7}` (fix hiệu lực), `AI` soft, `rules` ignore (canonical eval-overlay = quy tắc).
7. **App wiring (code):** headline `D_surface_v2 (hard-tier)` + `by_tier` + `term_policy` provenance; overlay span mang `constraint_strength`; no-recompute giữ.
8. Tự chạy `pipeline/tests app/backend/tests` → **261 passed**.

**Caveat trung thực (KHÔNG chặn PASS, nhưng quan trọng cho luận văn):** lists được biên SAU khi đã nhìn benchmark 4 chương. *Criterion* là a-priori + classifier không đọc output (vượt qua bar tuning-on-test theo nghĩa chặt), NHƯNG để đóng hoàn toàn nghi ngờ "researcher degrees of freedom" nên **validate policy trên chương HELD-OUT** (~18 chương D2L ngoài 4 chương chấm) → chứng minh không overfit. Là bước rigor khuyến nghị TRƯỚC khi dùng hard-tier headline làm số BẢO VỆ (hiện đã gắn `headline_ready=false` nên chưa over-claim). Khớp memory `dont-tune-intervention-on-test-baseline` ("khóa policy a-priori / trên dev").

**Follow-up (fix-forward, không chặn):**
- `term_policy.paths` trong report nhúng đường dẫn **TUYỆT ĐỐI** (`C:\Users\nguye\…`) → non-portable + lộ username khi commit; đổi sang relative.
- (mang từ EV_D2L_02) thêm 1 dòng `limitations` cho B (mask + cần `--gold-variants` mới so v1).
- `differentiation`/`derivatives` cùng share form "vi phân" → leakage tiềm tàng giữa 2 hard-term; theo dõi ở `ALIGN_PILOT_01`.
- Bỏ filter `term_is_injection_eligible` khỏi D = CHỦ ĐÍCH (chấm S0 & S1 trên cùng tập term → so sánh sạch hơn), minh bạch qua `all_terms`/`by_tier`; ghi nhận, không sửa.
