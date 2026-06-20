# TASK_EV_D2L_07b_localizer_cascade — Cascade localizer: T1 deterministic (registry, unique-only) → T2 LLM concept-fallback (blind, per-config) → T3 human audit; **eval/localization-only, KHÔNG đụng Translator/Builder/output dịch/D-scorer headline**

- **Status:** READY (Claude, 2026-06-20) — chờ CodeX §5 + REVIEW; KHÔNG commit.
- **Refs:** EV_D2L_07a (longest_match winner; SimAlign loại; gold 118), `surface_match.allocate_spans`, `localizer.localize_longest_match`; memory `simalign-rejected-for-term-localization`, `verify-on-committed-artifacts-not-reports`, `dont-tune-intervention-on-test-baseline`, `scoring-scope-equals-production-scope`, `token-growth-halt-and-audit`. 3-bên Claude+CodeX+GLM-5.2 hội tụ 2026-06-20.

## 1. Bối cảnh & mục tiêu *(Claude)*
longest_match (registry string-match) phủ ~93% nhưng có **trần cứng**: tập candidate = từ điển Builder → (a) **thiên vị config nghe-lời** (S1 luôn tìm được, S0 tự-do hay rớt — họ lỗi `scoring-scope-equals-production-scope`), (b) **mù với rendering mới** (TH2: S0 dịch "tập dữ liệu MNIST" không có trong registry → chỉ bắt được "MNIST"). String-match KHÔNG BAO GIỜ giải được TH2 (không khớp được chuỗi nó không biết).

Mục tiêu: **localizer phân tầng, config-neutral** để chấm consistency S0↔S1 công bằng:
- T1 deterministic phủ đa số (rẻ, 0-API, high-precision).
- T2 LLM chỉ xử lý **residual có flag** (hiểu ngữ nghĩa cho cụm mới), token-bounded.
- T3 human audit phần T2 + mẫu.

KHÔNG tăng accuracy bằng cách tune trên 118 gold (đã thành dev). KHÔNG dùng SimAlign (đã loại: 0.19 exact, nặng). KHÔNG dùng cặp S0↔S1 để localize cho metric (circular).

## 2. Scope
**Trong:** module cascade localizer (eval-only); preflight cost estimator; metric reporter tách code/LLM + per-config; dev/test split; guard tests.
**Ngoài (LOCK):** KHÔNG sửa Translator/Builder/registry/bản dịch frozen/D-scorer headline; KHÔNG inject gì vào runtime. Batch T2 = follow-up có điều kiện (L9). Builder-v2 = EV-08 riêng.

## 3. Spec *(Claude)*

### T1 — deterministic (0-API)
- Candidate = `canonical ∪ allowed_variants ∪ forbidden_variants` **của ĐÚNG entry registry theo chương chứa block** (registry per-chapter — verified: MNIST allowed=['MNIST']@introduction vs ['dataset MNIST']@linear_networks; KHÔNG gộp toàn cục).
- Localize trên target qua `allocate_spans`.
- **Resolve CHỈ khi UNIQUE match** trong target → nhãn `known_allowed` / `known_forbidden`.
- `multiple` / `partial` (chỉ là sub-string của một candidate dài hơn ĐÃ BIẾT) / `none` → **escalate T2**.
- **Position-anchor (vị trí tương đối nguồn↔đích) CHỈ để khoanh window cho T2, KHÔNG phải tiêu chí resolve của T1** (position không confirm được — reorder/gộp/tách).

### T2 — LLM concept-fallback (GPT OpenAI, temp=0, có cache)
- **Per-config ĐỘC LẬP** (mỗi request chỉ 1 arm). CẤM đưa S0+S1 cùng request (circular: span nuôi chính metric so S0↔S1). CẤM dùng context_diff/cặp làm localizer nuôi metric (chỉ cho discovery).
- **Prompt BLIND:** chỉ `{source occurrence được đánh dấu, source context, target window/block, occurrence_id}`. **KHÔNG gửi canonical/allowed/forbidden tiếng Việt** (chống anchor về registry — đúng mục đích T2 là tìm cụm MỚI). KHÔNG nhãn S0/S1, không "có/không memory".
- **Adaptive window:** (1) cửa sổ quanh vị trí tương đối + token-cap; (2) không thấy → mở rộng 1 lần; (3) vẫn không → cả block; (4) vẫn không → `human_required`.
- **Output JSON bắt buộc:** `{occurrence_id, status: localized|omitted|ambiguous|not_found, target_quote, start, end}`.
- **Code validate sau nhận:** `target_quote == target_text[start:end]`; offset hợp lệ; occurrence không claim trùng; quote xuất hiện nhiều chỗ mà offset không phân định → **reject → human**. (String-check chỉ chứng minh không-bịa-chuỗi, KHÔNG chứng minh mapping đúng → vẫn cần T3.)
- **Phân loại SAU localize:** returned span ↔ registry → `allowed`/`forbidden`/`novel`. LLM làm localization, code làm phân loại.

### Cache — HAI lớp
- **API replay cache:** key = toàn bộ messages request (per-case/batch).
- **Localization result cache:** key = `model + prompt_version + config + block_hash + occurrence_id + target_window_hash` (occurrence-level). Re-run: đọc result-cache từng occurrence → chỉ occurrence chưa-cached vào request mới → parse → lưu riêng từng occurrence.

### Preflight (0-API, TRƯỚC khi tốn tiền)
1. T1 chạy toàn chương (0-API) → đếm **residual thật** per-config.
2. Render **prompt đại diện thật** → đếm token thật → ước tính cost (KHÔNG tin "$3–5" chay).
3. Chạy T2 trên **dev-sample phân tầng CÓ NHÃN NGƯỜI** (S0/S1 × {none/multiple/partial} × {block ngắn/dài} × {hard/soft/preserve}) → đo accuracy T2 + agreement. (Dev-sample + chương held-out = 2 lần annotate — chi phí thật.)
4. Chạy toàn chương CHỈ KHI preflight dưới **budget đã khoá**.

### Metric reporter — tách + per-config
Báo riêng, KHÔNG gộp thành 1 số mờ: per-config **{exact_coverage, llm_coverage, unresolved}** cho S0 và S1 RIÊNG; `D_surface_exact` (code-only, đối xứng) **VÀ** `D_hybrid` (kèm LLM); `fallback_rate` per-config; `LLM↔human agreement` theo config & theo tier; token/cost. **Headline consistency báo trên CẢ exact lẫn hybrid; nếu lệch → cờ caveat (LLM gánh chính + bất đối xứng S0/S1 đáng kể).**

## 4. Acceptance criteria *(lệnh chạy + LOCK)*
```
python -m pytest -p no:cacheprovider pipeline/tests/test_localizer_cascade.py -v
#   test_t1_resolves_only_unique            : >=2 match -> escalate (KHÔNG pick first)
#   test_t1_candidates_from_correct_chapter : MNIST dùng entry introduction, KHÔNG gộp linear_networks
#   test_t1_classifies_allowed_forbidden_none
#   test_t2_prompt_is_blind                 : grep payload — KHÔNG canonical/allowed/forbidden VI, KHÔNG nhãn S0/S1
#   test_t2_per_config_independent          : 1 request KHÔNG chứa cả S0 và S1
#   test_t2_output_validated                : quote==text[s:e]; offset sai/ambiguous -> reject->human
#   test_t2_classify_after                  : returned span -> allowed/forbidden/novel
#   test_two_cache_layers                   : result-cache occurrence-level reuse khi đổi 1 item
#   test_preflight_zero_api                 : preflight không gọi API; trả residual count + token est
#   test_metrics_split_per_config           : report có S0/S1 riêng + exact vs hybrid
#   test_determinism                        : temp=0 + cache -> 2 lần chạy span identical
#   test_no_pair_localization_for_metric    : context_diff/joint KHÔNG nuôi D_hybrid
```
**LOCK:**
- **L1 T1 resolve UNIQUE-only;** position-anchor chỉ là window-hint cho T2.
- **L2 candidate per-chapter** (theo block_ids), không gộp toàn cục.
- **L3 T2 per-config độc lập** (chống circular); KHÔNG cặp S0↔S1 nuôi metric.
- **L4 T2 prompt BLIND** (không VI registry forms, không nhãn arm); phân loại allowed/forbidden/novel SAU.
- **L5 validate output LLM** (quote==offset, no double-claim); string-check≠mapping-đúng → **T3 human audit bắt buộc, mẫu RIÊNG S0 và S1**.
- **L6 HAI lớp cache** (API replay request-keyed + result occurrence-keyed); temp=0.
- **L7 Preflight 0-API + cost gate + held-out gate;** KHÔNG tune trên 118 (dev); đánh giá MỘT lần trên chương held-out mới annotate.
- **L8 metric tách code/LLM + per-config;** headline trên exact ∧ hybrid; bất đối xứng S0/S1 phải hiện.
- **L9 batch = follow-up có điều kiện** (interface `list[Occurrence]`, runtime per-case); chỉ bật khi pilot per-case chứng minh token/latency đáng tối ưu; pilot so per-case vs batch trên accuracy trước khi khoá.
- **L10 eval/localization-only:** KHÔNG đụng Translator/Builder/registry/frozen-output/D-scorer headline. Bỏ hardcode `DOCUMENTED_LIMITATION_ROWS` → class tổng quát trong dữ liệu eval.

## 5. Implementation notes *(CodeX điền — KHÔNG commit)*
<!-- files changed / implemented / deviation / commands / not run -->

## 6. Review *(Claude điền)*
<!-- Claude: tự chạy test, blind-prompt thật, per-config độc lập, 2 cache, preflight 0-API, metric tách, không tune-on-118, held-out. -->
