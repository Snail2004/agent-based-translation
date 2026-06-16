# TASK_EV_D2L_02_scorer_hygiene — Vệ sinh deterministic D-scorer (longest-match + mask URL/code + honor case_sensitive + full per-term) + đổi tên D_surface_v2, 0 API chấm lại output cũ

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (pp) · (nn).6 (trục D nhìn-được) · (ii) scope=scope · memory `d2l-scorer-validity-and-remediation-ladder`, `dont-tune-intervention-on-test-baseline`
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

Audit 2026-06-17 (3 bên, trên DB thật `data/jobs/d2l_p1/memory.sqlite3` + report `data/reports/d2l_translation_metrics.json`): **highlight overlay VÀ metric D dùng CHUNG một logic block-level surface-match, KHÔNG alignment.** `_score_registry_consistency` lấy mọi block có chuỗi source rồi đếm form registry xuất hiện BẤT KỲ ĐÂU trong target của block — Translator chỉ trả `output_text` (không alignment) nên không có tương ứng source→target thật. Vì highlight==metric (chung logic), **sửa scorer là sửa luôn highlight.**

Đo được **≥70% trong 213 "drift" của S1 là artifact đo, không phải dịch sai:**
- **125 substring-nesting:** canonical ⊂ allowed-variant (vd `chính xác` ⊂ `độ chính xác`; `truyền ngược` ⊂ `lan truyền ngược`) → form dài khớp kéo theo form ngắn → **luôn ≥2 form → drift bảo đảm** dù dịch nhất quán.
- **URL/case (vd `AI`):** scorer casefold + coi `.` là ranh giới từ → "ai" trong `discuss.d2l.ai` khớp term "AI". Thực đo **69/80 block "AI" là khớp NHẦM trong `d2l.ai`**, chỉ 9 block có "AI" thật.
- **25 cross-term leakage:** form của từ khác bị gán nhầm (vd `calculus` báo drift vì `phép tính` — bản dịch ĐÚNG của `calculations` cùng block — bị Builder liệt nhầm vào allowed_variants của calculus).

Cột `glossary_entries.case_sensitive` và `evidence_span_ids_json` ĐÃ có trong schema nhưng scorer **không đọc** (case_sensitive=0 cho cả 1608 term). Overlay chỉ nhận `worst_terms` (drift+undetected) → term `consistent` **không tới overlay → hiện xanh-dương "unscored", màu xanh-lá `consistent` gần như BẤT KHẢ HIỆN.**

**Mục tiêu (Cấp 1 của thang remediation, xem (pp)):** sửa cách ĐẾM/KHỚP của scorer ở mức **deterministic, 0 API**, đồng thời hạ nhãn metric → **`D_surface_v2`** (diagnostic block-level, KHÔNG phải số headline để bảo vệ). **Chấm lại trên `translation_runs` d2l_p3 hiện có, KHÔNG re-translate.** KHÔNG kỳ vọng làm calculus xanh — leakage cross-term cần dọn glossary (EV_D2L_03), không thuộc Cấp 1.

## 2. Scope

- **IN:**
  1. **Longest-match + non-overlap** khi đếm form trong `_score_registry_consistency` → diệt nesting (125 ca).
  2. **Mask non-prose trước khi khớp** (cả source-side `_count_source_matches` lẫn target-side dò form): URL, inline-code `` `...` ``, RST role `` :role:`...` ``, math `$...$`/`:math:`, code-fence, label/ref → thay bằng khoảng trắng GIỮ offset. Diệt AI/`d2l.ai`.
  3. **Honor `glossary_entries.case_sensitive`** (=1 → khớp phân biệt hoa-thường). Hạ tầng đúng dù Builder hiện để 0 hết (việc set giá trị thuộc EV_D2L_03).
  4. **Full per-term report** (mọi term + status + forms_used), không chỉ `worst_terms[:30]` → overlay tô được `consistent` (xanh-lá) thật, hết "xanh-dương unscored" giả.
  5. **Đổi nhãn metric:** `metric_version` → `d2l_translate_score_v2`; thêm `method:"block_surface_v2"`, `alignment:false`, `headline_ready:false`; mở rộng `limitations` (3 artifact + ghi rõ leakage calculus-type CHƯA fix ở đây). Cập nhật reader `thesis_scores.py` + nhãn UI thành "**detected target surface in this block**".
  6. **Chấm lại 0 API** trên DB thật (experiment `d2l_p3`, 4 chương) → report v2; in **delta drift trước/sau**.

- **OUT (để không lan man):**
  - `constraint_strength` (hard/soft/preserve/entity/ignore) + dọn glossary (bỏ `phép tính` khỏi calculus, `rules→quy tắc`) → **EV_D2L_03**.
  - Word alignment SimAlign/awesome-align → **ALIGN_PILOT_01**.
  - Trục adequacy (COMET/judge) → task riêng (tái dùng judge sẵn có).
  - **RE-TRANSLATE / mọi API call** — CẤM. Chỉ re-score.
  - Sửa cross-term leakage (calculus) — KHÔNG thuộc Cấp 1; calculus VẪN drift sau task (ghi rõ).
  - Dùng output S0 để chọn term — CẤM (bẫy tuning-on-test, memory `dont-tune-intervention-on-test-baseline`).

## 3. Spec *(Claude viết)*

**`pipeline/eval/d2l_translate_score.py`**
- Thêm `_mask_non_prose(text) -> str`: thay nội dung URL (`https?://\S+`, `\b\w+\.(ai|io|com|org)\S*`), inline-code (`` `[^`]*` ``), RST role (`` :[a-z]+:`[^`]*` ``), math (`\$[^$]*\$`), label/ref bằng space CÙNG ĐỘ DÀI (giữ offset). Áp dụng cho text trước MỌI khớp.
- `_count_source_matches(text, source_term, *, case_sensitive=False)`: chạy trên text đã mask; `case_sensitive=True` → không casefold.
- Trong `_score_registry_consistency`: thay vòng `for candidate: if _has_vi(...)` độc-lập bằng **gán longest-match non-overlap**: sort candidates theo độ dài giảm dần; quét target đã mask; candidate khớp ở vị trí CHƯA bị span trước chiếm → `forms_used[candidate]+=1` + đánh dấu span đã dùng; bỏ qua khớp chồng lấn. Honor `case_sensitive` theo row. Giữ semantics "đếm theo block" (mỗi block tối đa +1/candidate).
- `_has_vi(text, needle, *, case_sensitive=False)`: chạy trên text đã mask (vẫn dùng cho B presence).
- Report: `metric_version="d2l_translate_score_v2"`; trong `D_registry_consistency[config]` thêm `method/alignment/headline_ready`; thêm `terms_all` (list MỌI term: source_term/status/forms_used/source_blocks) đủ cho overlay tô consistent; mở rộng `limitations`.

**`app/backend/services/thesis_scores.py`**
- `_d2l_drift`: đọc `terms_all` (không chỉ `worst_terms`) → trả luôn term `consistent` (status đính kèm) cho `_score_index` overlay → overlay tô xanh-lá. GIỮ no-recompute (chỉ copy từ report). Thêm nhãn headline `D_surface_v2` + cờ `alignment:false`.

**`app/backend/services/thesis_overlay.py` + `app/prototype/{app,parts_center}.jsx`**
- Nhờ `terms_all`, span `consistent` có detail → render `hl-status-consistent` (xanh-lá). Đổi wording card/legend: "detected target surface in this block (surface match, not alignment)".

**Ràng buộc (trích LOCK):** observe⊥compute (nn).1 — UI KHÔNG recompute, scorer là nguồn số duy nhất. scope=scope (ii) — denominator = block đã dịch trong scope, không đụng passthrough/code/math. App đọc SQLite `mode=ro`. Toàn task 0 API.

## 4. Acceptance criteria *(lệnh chạy được)*

```bash
cd THESIS_RUNTIME_TOOL

# (1) Unit tests mới
python -m pytest pipeline/tests/test_d2l_translate_score.py -v   # PASS, gồm:
#   test_longest_match_no_nesting : term có canonical⊂variant KHÔNG đếm trùng span
#   test_url_masked_no_false_term : "see https://discuss.d2l.ai/t/39" KHÔNG tạo occurrence "AI"
#   test_case_sensitive_acronym   : case_sensitive=1 → "AI" không khớp "ai" thường
#   test_terms_all_present        : report có terms_all gồm cả status=consistent

# (2) Re-score 0 API trên DB thật (KHÔNG translate) → report v2
python -m pipeline.scripts.score_run \
  --db data/jobs/d2l_p1/memory.sqlite3 \
  --experiment d2l_p3 \
  --chapters d2l_introduction d2l_preliminaries d2l_linear_networks d2l_multilayer_perceptrons \
  --config S1 \
  --out data/reports/d2l_translation_metrics_v2.json
# → metric_version == "d2l_translate_score_v2"

# (3) Delta drift trước/sau (dán số vào §5) — kỳ vọng:
#   drift_terms S1 GIẢM mạnh (nesting + AI-url biến mất); undetected ~giữ
#   "AI" source_blocks: 80 → ~11 (hết d2l.ai)
#   "độ chính xác"/"backpropagation"-kiểu nesting → consistent
#   "calculus" VẪN drift (leakage — đúng kỳ vọng, chờ EV_D2L_03)

# (4) Full regression, không tốn API
python -m pytest pipeline/tests app/backend/tests -q   # xanh, không regression
```

## 5. Implementation notes *(CodeX điền)*

## 6. Review *(Claude điền)*
