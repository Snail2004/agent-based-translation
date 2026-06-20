# TASK_EV_D2L_07b_localizer_cascade — Cascade localizer: T1 deterministic (runtime registry, unique-only, conflict-aware) → T2 LLM concept-fallback (blind, per-config) → T3 human audit; **eval/localization-only; 118 = DEV/regression (KHÔNG generalization); held-out = task EV-07c riêng**

- **Status:** REVIEW (CodeX, 2026-06-21) — implemented through the bounded 8-case DEV pilot only; no whole-chapter API run; KHÔNG commit.
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

Implemented 2026-06-21, stopped at the user-approved 8-case DEV gate.

### Files / implementation
- Added `pipeline/eval/localizer_cascade.py`:
  - runtime-DB registry resolver with chapter-specific precedence and global fallback;
  - T1 unique-only localization, cross-term no-double-claim, match-level allowed/forbidden conflict handling, short-known-form residuals;
  - opaque per-config T2 prompt, position target window, strict JSON validation, unique-quote and position-quote re-anchoring;
  - API replay cache through the existing `LLMClient` plus occurrence-level result cache with source/context/window hashes;
  - DEV preflight, cost gate, per-config DEV metrics and T3 audit CSV/HTML.
- Added `pipeline/scripts/localizer_cascade.py` with `--preflight --dev` and cost-token-gated `--run-dev --dev` modes.
- Added pinned `pipeline/configs/llm_localizer.yaml`: `gpt-5.4-mini-2026-03-17`, temperature `0`, seed `20260621`, reasoning `none`, max output `256`, prompt cap `2000`, pilot daily cap `100000`.
- Added `pipeline/tests/test_localizer_cascade.py` (11 tests).
- Removed the term-specific `DOCUMENTED_LIMITATION_ROWS[mt_mnist:S1]` exception from `localizer.py`; regression gates are now data-declared via `edge_kind`, while ordinary misses remain visible as limitations.
- Generated `data/reports/localizer_cascade_preflight.json`, `data/reports/localizer_cascade_dev.json`, and `data/eval/localizer_cascade/{audit_dev.csv,audit_dev.html}`. SQLite caches are local/ignored.

### Explicit implementation decisions / deviations
- The DB has 122 entries with some allowed∩forbidden overlap. The global count is reported, but an occurrence escalates as `registry_conflict` only when the **matched form** is conflicted; an unambiguous canonical match from the same entry may still resolve.
- The requested metric reporter is kept as **DEV localization accuracy**, not labeled `D_hybrid`: eight representative spans cannot validly recompute corpus consistency, and L10 forbids changing the D headline. `D_hybrid` remains blocked pending T3/EV-07c held-out validation.
- Adaptive expansion is estimated in preflight but not exercised in this bounded pilot: six target blocks fit the full 700-char window and both position windows contained the human gold. No second/third-stage API calls were needed or allowed.
- Batch remains unimplemented per L9.
- Provider returned no `system_fingerprint` (`None`); exact model/config and replay keys are still recorded. Cold-call byte determinism is not claimed.

### Offline preflight
Command: `python -m pipeline.scripts.localizer_cascade --preflight --dev`.
- DEV gold: 118 rows; legacy longest failures: exactly 8.
- T1 conservative DEV result: 78 resolved / 40 residual (`multiple=20`, `short_known_form=16`, `registry_conflict=3`, `none=1`). This 33.9% fallback rate is a DEV warning; it was **not** extrapolated as a whole-book result.
- Prompt v2 estimate for 8 cases: 4,443 prompt + 2,048 max output = 6,491 token; estimated `$0.00521`; three-stage worst case 19,473 token / `$0.01562`.
- Prompt audit: target form lists and arm labels absent; all eight computed windows contained their DEV gold spans.

### Bounded GPT pilot (DEV only)
Two prompt passes were run on the same eight DEV cases, then prompt tuning stopped to avoid further overfit:
1. `d2l_localizer_t2_v1`: 8 cold calls, 3,438 prompt + 359 completion token, `$0.0015775`. Raw model offsets were unreliable; before code re-anchor exact was 1/8, position/unique re-anchor raised the same cached responses to 4/8 with zero new calls.
2. `d2l_localizer_t2_v2`: one general boundary revision (all marked content words must be represented; exclude unmarked neighbors/complements), 8 cold calls, 3,894 prompt + 360 completion token, `$0.0016935`. Final result: **5/8 exact-span** (`S0 3/6`, `S1 2/2`), 8/8 returned existing target strings, no hallucinated quote.

Total cold API use across both DEV prompt passes: **16 requests over the same 8 cases**, 7,332 prompt + 719 completion = 8,051 token, `$0.003271`. No chapter/full-corpus run.

Final v2 failures, retained rather than tuned away:
- `elementwise multiplication`: correct semantic head but over-extended with `of two matrices` rendering;
- `membership`: correct semantic head but over-extended with the complement `in a set`;
- `target`: wrong ownership — selected `nhãn`, the rendering of adjacent unmarked `label`, instead of `mục tiêu`.

Conclusion: T2 is useful for off-registry localization and position re-anchoring fixes the offset layer, but **5/8 exact is not trustworthy enough for an unaudited metric**. T3 human remains mandatory; EV-07c held-out is required before any generalization claim.

### Verification
- `python -m pytest -p no:cacheprovider pipeline/tests/test_localizer_cascade.py -q` -> 11 passed.
- `python -m pytest -p no:cacheprovider pipeline/tests/test_localizer.py pipeline/tests/test_localizer_cascade.py -q` -> 24 passed.
- `python -m pytest -p no:cacheprovider pipeline/tests app/backend/tests -q` -> **326 passed in 110.10s**.
- Commands exited 0; Windows emitted only the known pytest temp-cleanup `PermissionError` after completion.
- Frozen DB SHA-256 unchanged: `DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8`.
- No commit/push.

## 6. Review *(Claude)* — VERDICT: PASS (commit, no push)

Tự re-derive trên artifact ĐÃ COMMIT (không tin report — [[verify-on-committed-artifacts-not-reports]]):
- **5/8 exact-span** tự tính lại từ `localizer_gold.csv` = 5/8 (khớp report). **0/8 hallucination**: cả 8 `quote == target_text[start:end]`.
- 3 FAIL là lỗi thật/arguable, không sai âm thầm — giữ lại không tune: `elementwise_multiplication:S0` nuốt thừa "của hai ma trận"; `membership:S0` nuốt thừa "về một tập hợp"; `target:S0` chọn nhầm "nhãn"(label) thay "mục tiêu"(target) ở vùng khác hẳn (ownership error thật).
- **T1** tự chạy trên DB thật: 78/118 resolved, residual reasons `{multiple:20, short_known_form:16, registry_conflict:3, none:1}`; `registry_conflict_count=122`; `legacy_longest_failures=8` — tất cả khớp.
- **DB frozen** SHA-256 first16 = `DA0F687894090D43` — KHÔNG đổi.
- **Full suite tự chạy lại: 326 passed** (PermissionError chỉ là cleanup temp sau run, không phải test fail).

Refactor `localizer.py` (gỡ hardcode `DOCUMENTED_LIMITATION_ROWS` + set `{MNIST dataset, machine learning}`) — đụng scorer EV-07a đã đóng nên verify riêng: tự chạy `score_localizer_bakeoff` trên gold đã commit → **recommendation = longest_match BẤT BIẾN**, `longest_match.regression_fail=[]`, eligible=True. Gate `edge_kind` chỉ kích trên simalign (đã loại). `short_known_form` (vd MNIST) chuyển thành quy tắc data-driven trong T1 — đúng yêu cầu "logic chứ không cấp đáp án sẵn".

Ghi nhận minh bạch (không chặn PASS):
1. Cả 8 `offset_source` là re-anchor (unique/position), KHÔNG ca nào dùng offset model trả về → model chỉ đóng góp QUOTE, code định vị offset (đúng thiết kế model=ngữ nghĩa / code=offset), nhưng nhánh model-offset chưa được pilot này kiểm chứng.
2. Report dev commit là REPLAY (`api_cache_hits=8`, `cost_usd=0`); chi phí thật `$0.003271`/16 req ghi ở §5 + preflight — đừng đọc cost=0 là "miễn phí".
3. `localizer_bakeoff.json` (EV-07a) KHÔNG regenerate → desync nhẹ bucket-label với code mới, nhưng recommendation bất biến nên không ảnh hưởng kết luận đã đóng.

Đồng thuận §5: T2 hữu ích cho off-registry + position-reanchor sửa được tầng offset, nhưng **5/8 chưa đủ tin để tự nuôi metric** — T3 human bắt buộc, EV-07c held-out bắt buộc trước mọi tuyên bố generalization. KHÔNG tune thêm trên DEV.

---
## Follow-on: TASK_EV_D2L_07c_heldout (stub)
Dịch 1 chương D2L MỚI (chưa có trong 4 chương) bằng experiment_id riêng (S0 + S1), annotate localization gold, chạy cascade **một lần** → số generalization thật. Tách khỏi 07b vì 07b cấm re-translate và 118 đã là dev.
