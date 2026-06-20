# TASK_EV_D2L_07b_localizer_cascade — Cascade localizer: T1 deterministic (runtime registry, unique-only, conflict-aware) → T2 LLM concept-fallback (blind, per-config) → T3 human audit; **eval/localization-only; 118 = DEV/regression (KHÔNG generalization); held-out = task EV-07c riêng**

- **Status:** READY-v2 (Claude, 2026-06-21, sau REWORK theo CodeX DB-verify) — chờ CodeX §5 + REVIEW; KHÔNG commit.
- **Refs:** EV_07a (longest winner; SimAlign loại; gold 118), DB `data/jobs/d2l_p1/memory.sqlite3` table `glossary_entries`; memory `verify-on-committed-artifacts-not-reports`, `simalign-rejected-for-term-localization`, `dont-tune-intervention-on-test-baseline`, `scoring-scope-equals-production-scope`. 3-bên Claude+CodeX+GLM hội tụ.

## 1. Bối cảnh & mục tiêu *(Claude)*
longest_match phủ ~93% (108/116) nhưng có trần: candidate = từ điển Builder → thiên-vị-config (S1) + mù với rendering mới (TH2). **Đã verify trên DB runtime** (KHÔNG phải prepass): registry là **GLOBAL** (`scope=global, chapter_id=NULL`); "tập dữ liệu MNIST" nằm trong `forbidden` của `gl_mnist_dataset`. **Mở rộng candidate sang full registry KHÔNG cải thiện** (108→108: fix MNIST/target qua forbidden nhưng vỡ `deletion`→"điền khuyết" do candidate thừa khớp nhầm) → bằng chứng phải **resolve unique-only + escalate khi mơ hồ**, không nhồi candidate.

Mục tiêu: localizer phân tầng config-neutral để chấm S0↔S1 công bằng. **118 = DEV/regression** (đã thiết kế trên nó → KHÔNG dùng làm số generalization). Số generalization = **EV-07c** (chương held-out mới).

## 2. Scope
**Trong:** cascade localizer eval-only; preflight cost estimator; metric reporter tách code/LLM + per-config; chạy DEV trên 118; guard tests.
**Ngoài (LOCK):** KHÔNG sửa Translator/Builder/registry/frozen-output/D-scorer headline; KHÔNG re-translate. **Held-out = TASK EV-07c riêng** (dịch 1 chương D2L mới bằng experiment_id riêng → annotate → chấm MỘT lần). Batch T2 = follow-up có điều kiện (L9).

## 3. Spec *(Claude)*

### T1 — deterministic (0-API), nguồn candidate = RUNTIME DB
- **Scope resolver (B1):** lấy entry từ `glossary_entries` theo `source_term`: nếu có chapter-specific entry khớp `chapter_id` của block → dùng nó; nếu không → entry `scope=global`; nhiều entry ngang cấp/xung đột → `residual`. KHÔNG dựng lại từ prepass JSON.
- Candidate = `canonical ∪ allowed_variants ∪ forbidden_variants` của entry resolved.
- **Conflict policy (B2):** form nằm CẢ allowed∩forbidden (122 entry trong DB) → trạng thái `registry_conflict` → **escalate T2/human**, KHÔNG tự chọn bên nào; report tổng số conflict (đây là hygiene Builder, không phải lỗi localizer).
- **UNIQUE = occurrence-level:** T1 resolve CHỈ khi có **đúng một** span khớp cho occurrence đó, VÀ **không hai source-term nào nhận chung một target span** (no-double-claim). Nhãn: `known_allowed`/`known_forbidden`.
- `multiple` / `short_known_form` (span là sub-string của một candidate dài hơn ĐÃ BIẾT) / `ambiguous_context` / `registry_conflict` / `none` → **escalate T2**. (KHÔNG claim code biết đó là "partial translation".)
- Position-anchor (vị trí tương đối) CHỈ để khoanh window cho T2, KHÔNG phải tiêu chí resolve.

### T2 — LLM concept-fallback (OpenAI GPT)
- **Per-config độc lập** (1 arm/request) → chống circular (span nuôi chính metric S0↔S1). KHÔNG cặp/context_diff nuôi metric.
- **Prompt BLIND:** chỉ `{source occurrence đánh dấu, source context, target window/block, occurrence_id}`. KHÔNG gửi canonical/allowed/forbidden VI (chống anchor); không nhãn S0/S1.
- **Adaptive window:** cửa sổ quanh vị-trí-tương-đối + token-cap → không thấy: mở rộng 1 lần → cả block → `human_required`.
- **Output JSON:** `{occurrence_id, status: localized|omitted|ambiguous|not_found, target_quote, start, end}`. Code validate: `quote==target_text[start:end]`, offset hợp lệ, no-double-claim; quote nhiều chỗ mà offset không phân định → reject→human. (String-check ≠ mapping đúng → vẫn cần T3.)
- Phân loại SAU: returned span ↔ registry → allowed/forbidden/novel.
- **Pin đầy đủ (xác định):** `model, seed, reasoning_effort, max_output_tokens, prompt_version`. `temperature=0` KHÔNG đảm bảo cold-call byte-identical → **chỉ replay-cache mới tái lập**; ghi rõ.

### Cache — HAI lớp
- **API replay cache:** key = toàn bộ messages request.
- **Result cache:** key = `model + prompt_version + config + block_hash + occurrence_id + source_occurrence_hash + source_context_hash + target_window_hash` (occurrence_id đơn độc có thể stale nếu source span đổi → phải thêm hash).

### Preflight (0-API)
1. T1 toàn chương (0-API) → residual count per-config + **conflict count**.
2. Render prompt thật → token thật → cost, **gồm worst-case adaptive expansion** (window nhỏ → rộng → full block), không chỉ 1 request/case.
3. Chạy T2 trên DEV (118 đã có gold người; chỉ annotate THÊM nếu preflight corpus lộ loại residual hoàn toàn mới).
4. Toàn corpus CHỈ khi dưới budget đã khoá.

### Metric reporter — tách + per-config (DEV)
per-config {exact_coverage, llm_coverage, unresolved, registry_conflict} cho S0 và S1 RIÊNG; `D_surface_exact` (code-only) **VÀ** `D_hybrid`; fallback_rate per-config; LLM↔human agreement theo config & tier; token/cost. **Headline trên CẢ exact ∧ hybrid; lệch → cờ caveat (bất đối xứng S0/S1).** Ghi rõ mọi số 118 là **DEV/regression, không generalization.**

### T3 — human audit artifact
- Xuất CSV + HTML (như localizer_gold) cột: `occurrence_id, config, source_term, source_sentence, target_window, t2_status, t2_quote, t2_start, t2_end, human_verdict(accept/fix/reject), human_quote, human_start, human_end, note`. Merge bằng `occurrence_id`. Lấy mẫu audit **RIÊNG S0 và S1**.

## 4. Acceptance criteria *(lệnh + LOCK)*
```
python -m pytest -p no:cacheprovider pipeline/tests/test_localizer_cascade.py -v
#   test_registry_scope_resolver       : dùng global entry; chapter-specific override nếu có; conflict→residual
#   test_t1_unique_occurrence_only     : ≥2 span khớp → escalate; deletion-style không bị break
#   test_t1_no_double_claim            : 2 term KHÔNG nhận chung target span
#   test_registry_conflict_escalates   : form ∈ allowed∩forbidden → registry_conflict→T2 (report count=122-class)
#   test_t2_prompt_blind + per_config_independent
#   test_t2_output_validated + classify_after
#   test_result_cache_key_has_source_hash : đổi source span → cache miss (không stale)
#   test_preflight_zero_api + worstcase_expansion_cost
#   test_metrics_split_per_config + exact_vs_hybrid
#   test_118_is_dev_not_generalization : report gắn nhãn dev
python -m pipeline.scripts.localizer_cascade --preflight --dev   # 0-API: residual+conflict+cost
```
**LOCK:**
- **L1** T1 candidate từ **runtime DB** (scope resolver), KHÔNG prepass; KHÔNG gộp xung đột.
- **L2** T1 resolve **unique occurrence-level + no-double-claim**; còn lại escalate.
- **L3** `registry_conflict` (allowed∩forbidden) → escalate + report; là Builder hygiene.
- **L4** T2 per-config độc lập (chống circular); prompt BLIND (chống anchor); validate offset; string-check≠mapping → T3 bắt buộc, mẫu riêng S0/S1.
- **L5** pin model+seed+reasoning+max_out+prompt_version; xác định qua **replay-cache** (temp=0 chưa đủ). Result-cache key gồm source occ/context hash.
- **L6** preflight 0-API + worst-case expansion cost + **cost gate** trước API.
- **L7** **118 = DEV/regression, KHÔNG generalization**; held-out = **EV-07c riêng** (dịch chương mới). KHÔNG tune trên 118.
- **L8** metric tách code/LLM + per-config; headline exact ∧ hybrid; bất đối xứng phải hiện.
- **L9** batch = follow-up có điều kiện (interface list, runtime per-case) — pilot per-case vs batch trước khi khoá.
- **L10** eval/localization-only; bỏ hardcode `DOCUMENTED_LIMITATION_ROWS` → class tổng quát trong data.

## 5. Implementation notes *(CodeX — KHÔNG commit)*
<!-- files / impl / deviation / commands / not run -->

## 6. Review *(Claude)*
<!-- tự chạy test; scope resolver dùng DB thật; unique+no-double-claim; conflict escalate; blind+per-config; cache key; preflight 0-API; 118 nhãn dev. -->

---
## Follow-on: TASK_EV_D2L_07c_heldout (stub)
Dịch 1 chương D2L MỚI (chưa có trong 4 chương) bằng experiment_id riêng (S0 + S1), annotate localization gold, chạy cascade **một lần** → số generalization thật. Tách khỏi 07b vì 07b cấm re-translate và 118 đã là dev.
