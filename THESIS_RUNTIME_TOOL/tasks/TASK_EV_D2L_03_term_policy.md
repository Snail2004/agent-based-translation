# TASK_EV_D2L_03_term_policy — Phân tầng constraint_strength (a-priori) + dọn glossary ở tầng EVAL-overlay + headline D_surface_v2 = hard-only; 0 API re-score, KHÔNG re-translate

- **Status:** READY
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

## 6. Review *(Claude điền)*
