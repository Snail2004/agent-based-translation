# TASK_APP_A02_registry_overlay — Runtime registry overlay (highlight memory agent-xây ở CẢ gốc + dịch, tô theo drift)

- **Status:** DONE / PASS
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (nn).6 [trục D NHÌN-ĐƯỢC] + (nn).4 [provenance cấp-query, gold KHÔNG runtime] + (nn).5 [quarantine gold-authoring] | (kk) trục D xuyên-domain | APP-A01 (DatasetReadModel) + APP-D01 (D-consistency report = nguồn status/forms_used) | bộ nhớ scoring-scope (hình == số)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

Cockpit hiện surface registry agent-xây dưới dạng **đếm/liệt kê** (panel phải: "1608 terms"), nhưng **KHÔNG neo term vào text** → không highlight. Lý do code: `_glossary_to_runtime` trả `occurrences: []` (rỗng), nên `buildSpans` (app.jsx:159) không tô gì. Engine highlight (`buildSpans`/`segmentize`/`<mark>`/`HighlightHoverCard`) **vẫn còn sẵn** — chỉ thiếu dữ liệu occurrences.

Hover-card cũ kiểu AILAB (`ANNOTATOR/ALLOWED/FORBIDDEN/CONFIDENCE/VERIFIED/Edit/Review`, doc `gold_demo`) = **gold authoring = eval-only**, đã quarantine ĐÚNG (nn).5 — KHÔNG khôi phục.

**Mục tiêu:** làm cho **memory agent-xây NHÌN THẤY ĐƯỢC** trong text — highlight ở **CẢ bản gốc (EN) LẪN bản dịch (S0/S1, VI)**, **tô màu theo trạng thái drift** (trục D), hover ra **runtime card**. Đây là "trục D nhìn được" ở mức trực quan nhất + làm rõ đóng góp lõi của luận văn (agent-built memory). **0-API, deterministic, observe-only** — match term vào block là **dò chuỗi trong code** (mili-giây), KHÔNG gọi API, KHÔNG "chạy lại từng term".

## 2. Scope

- **IN — (A) Overlay computation (read-only, 0-API, deterministic):**
  - **Source spans:** mỗi glossary term runtime → match `source_term` (surface) trong `clean_text` từng block thuộc scope của term; **tái dùng `normalize_apostrophe`/anchor-match kiểu `plan_anchors`** để nhất quán với Builder. Điền `occurrences: [{block_id, span:[s,e]}]` (đang rỗng ở `_glossary_to_runtime`). Entity: điền `mentions: [{block_id, span, surface}]` từ `canonical_source` + aliases.
  - **Target spans (CẢ bản dịch):** per config (S0/S1) → match các cách dịch của term trong `block.translations[config].target_text`. **Tập biến thể = `forms_used` của SCORER** (D-report) cho term đó ⇒ tô đúng cái D-metric đã đếm (validity "hình == số"). Term không có chi tiết per-term trong report → fallback `target_term` + `allowed_variants` từ registry, **đánh dấu un-scored/neutral** (không tuyên bố status).
  - **Liên kết 2 phía:** cùng `term_id`/`entity_id` ⇒ hover một bên, cả 2 phía cùng sáng.
  - **Status drift:** gắn `status` (consistent/drift/undetected) **TỪ D-report** (`D_registry_consistency.worst_terms[].status`; aggregate ở chỗ không có per-term). KHÔNG tự tính.
- **IN — (B) Viewer (cockpit, observe-only):**
  - Đẩy occurrences/mentions vào đúng đường `buildSpans` sẵn có → `<mark>` trong **block gốc VÀ pane dịch S0/S1**; **màu theo status** (consistent / drift / undetected / unscored).
  - Hover → **runtime card**: `source_term → target_term`, biến thể `forms_used` + count, số lần xuất hiện, status, provenance="agent-built". **KHÔNG** field gold (không annotator/allowed/forbidden/VERIFIED/Edit/Review).
  - Hover một term → sáng cả 2 phía (link `term_id`).
  - Khôi phục **icon nhãn cột trái dạng RUNTIME** (block có glossary / có entity / có drift) — bản runtime thay cho icon annotation cũ.
- **OUT:**
  - **KHÔNG gold authoring** (annotator/allowed/forbidden/VERIFIED/Edit/Review giữ quarantine; gold KHÔNG bao giờ tô như runtime; gold KHÔNG vào overlay).
  - **KHÔNG recompute D-metric.** status + tập biến thể `forms_used` **ĐỌC từ scorer report**; char-span match chỉ là **tra-cứu-vị-trí để hiển thị** (như block-locate của D01), KHÔNG phải metric. Với term đã-scored, tập biến thể tô PHẢI == `forms_used` (KHÔNG bịa thêm biến thể).
  - **KHÔNG** đổi engine/scorer; **0-API**; **KHÔNG** đổi schema dataset 1.5.0 (occurrences tính lúc read-time, KHÔNG lưu DB).
  - Giữ tách read-model: matching (dataset/A01) ⊥ status/forms_used (scores/D01) — compose ở overlay/viewer, KHÔNG trộn schema.

## 3. Đầu mối dữ liệu *(Claude đã kiểm — imple khớp & ghi §5)*

- `buildSpans(block, glossary, entities)` (app.jsx:159) ăn `glossary[].occurrences:[{block_id,span}]` + `entities[].mentions:[{block_id,span,surface}]`; `<mark>`/`segmentize`/`HighlightHoverCard` ở parts_center.jsx (688 = glossary hover card).
- **Chỗ trống:** `_glossary_to_runtime` (thesis_readmodel.py ~141) trả `occurrences: []`. Item có `source_term`, `target_term`/`expected_target`, `allowed_variants`, `term_id`. Entity `_entity_to_runtime` tương tự.
- **Bản dịch per block:** `block.translations[config].target_text` (thesis_readmodel.py ~331; `_translation_to_readmodel`:207 map `output_text`→`target_text`). config key = S0/S1.
- **Nguồn status/biến thể (D01):** `data/reports/d2l_translation_metrics.json` `D_registry_consistency.{S0,S1}.worst_terms[].{source_term,target_term,status,forms_used,source_blocks}` (chi tiết chỉ top worst_terms; aggregate `consistent/drift/undetected` ở mức tổng). TI: `ecs.per_entity[].{forms_used,coverage,name_mention_blocks}`.
- **Logic match tái dùng:** `normalize_apostrophe` (pipeline eval thesis_scoring) + mẫu anchor `plan_anchors` (retrieval/context_builder).
- **Known data-limit (ghi rõ):** chi tiết per-term (status/forms_used) chỉ có cho `worst_terms`; term ngoài đó → highlight neutral, không tuyên bố status.

## 4. Acceptance *(0-API)*

1. Overlay điền **source occurrences** cho glossary d2l_p1 THẬT (non-empty); `clean_text[span[0]:span[1]]` khớp `source_term` (modulo normalize).
2. **Target occurrences** S1 dùng `forms_used` của scorer: term drift (vd `AI`→{`trí tuệ nhân tạo`,`AI`}) tô **cả 2 cách dịch** trong text dịch; status lấy từ report.
3. **VALIDITY (hình == số):** với một term đã-scored, tập biến thể tô ở target == đúng keys `forms_used` của term đó trong D-report (không dư biến thể bịa).
4. Hover card hiện field **runtime** (source→target, forms_used, status), **KHÔNG** field gold.
5. `term_id` link 2 phía (cùng id ở source span và target span).
6. **KHÔNG** gold term nào bị tô; **KHÔNG** đường nào recompute D; **0-API**.
7. Backend overlay test (Python) + browser smoke viewer; full regression xanh.
8. Dán output vào §5.

## 5. Implementation notes *(CodeX, 2026-06-16)*

- Backend overlay composer: thêm `services/thesis_overlay.py` + route `GET /api/thesis/overlay/<job_id>`.
  - Chỉ đọc SQLite `mode=ro`, 0 API, không đổi engine/scorer/schema.
  - Source spans tính read-time từ runtime `glossary_entries.source_term`; entity ưu tiên bảng `mentions`, fallback scan canonical/alias cho DB cũ.
  - Target spans lấy từ `translation_runs` theo config S0/S1. Với term đã có trong score report, tập biến thể tô đúng bằng keys `forms_used`; fallback từ registry bị gắn `unscored`.
  - Status copy từ ScoreReadModel/D-report, không tự tính lại D/TAR/ECS.
  - Match dùng apostrophe normalization, regex case-insensitive có word-boundary khi phù hợp, chọn non-overlap theo longest/leftmost.
- Performance rework sau smoke endpoint thật:
  - Full eager overlay ban đầu timeout >120s; sau index hóa vẫn ~17.5s, không đạt để dùng trong viewer.
  - Thêm scoped params `block_id` / `chapter_id`; overlay service đọc scoped blocks/translations trực tiếp từ SQLite thay vì materialize full DatasetReadModel.
  - Frontend không gọi full overlay trong lúc load thesis dataset nữa. UI load DatasetReadModel trước, rồi fetch overlay scoped theo selected block hoặc current chapter/book view.
  - Đo trên DB thật `d2l_p1` sau restart: block scope `0.107s`, chapter scope `2.151s`, full audit `16.939s`.
- Frontend wiring:
  - `applyRegistryOverlay()` merge source occurrences, target spans, status by config và block overlay counts vào viewer read-model.
  - Reuse `buildSpans` / `<mark>` cho source, thêm render `target_spans` trong `TranslationCompare`.
  - Thêm màu status: drift/low_coverage, undetected, consistent, unscored.
  - Link source/target theo shared `term_id`/`entity_id`.
  - Thêm runtime sidebar icons cho source overlay, target overlay và drift.
  - Runtime hover card ẩn field gold-authoring cũ, hiển thị source/target, `forms_used`, status, provenance, surface.
- Tests:
  - `python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL\app\backend\tests\test_thesis_overlay.py THESIS_RUNTIME_TOOL\app\backend\tests\test_thesis_readmodel.py -q` -> `6 passed`.
  - `python -m pytest -p no:cacheprovider THESIS_RUNTIME_TOOL\app\backend\tests -q` -> `130 passed`.
  - `git diff --check` -> no whitespace errors; chỉ có Windows LF->CRLF warnings.
  - Pytest vẫn in known Windows temp symlink cleanup `PermissionError` ở atexit, nhưng exit code 0 và test pass.
- Browser smoke:
  - Backend `THESIS_APP_MODE=cockpit`, prototype `http://127.0.0.1:8765/index.html`.
  - Page load `thesis:d2l_p1`, không có app console error; chỉ có Babel-standalone warning sẵn có.
  - D2L translated chapter/block smoke: `marks=8378`, `sourceMarks=3763`, `targetMarks=4615`, `driftMarks=451`, `runtimeBadges=950`, `translationCards=696`.
  - Screenshot evidence captured in Codex thread.
  - Limitation trung thực: Browser runtime trong phiên này không có Playwright `hover()` và sandbox chặn synthetic MouseEvent, nên chưa browser-proof được hover-card. Code path đã nối qua `onMouseEnter`; đề nghị Claude/manual review hover một lần trong UI.

## 6. Review *(Claude điền)*

- **Verdict:** PASS / ACCEPT. Implementer: CodeX. Re-verify độc lập từ code + DB thật, không tin §5. CodeX tự bắt lỗi perf 120s khi smoke thật rồi sửa scoped — đúng kỷ luật (lỗi này vô hình với unit test).
- **Re-verify:**
  - **"Hình == số" ✓ (gate cứng nhất):** `_target_forms_for_term/entity` — term đã-scored dùng `detail.forms_used.keys()` của report (`forms_source="score_report.forms_used"`, `scored=True`); term ngoài worst_terms fallback runtime + `scored=False`. status lấy từ report (`detail.status`), KHÔNG recompute. Test assert `forms_used=={"Agent":1}` + `forms_source` + `scored`.
  - **No gold leak ✓:** overlay chỉ đọc `glossary_entries`/`entities` (runtime), KHÔNG `eval_glossary_gold`. Test assert `"eval_glossary_gold"/"reference_eval_only"/"gold-1" not in serialized`. **Hover-card (CodeX nhờ verify) — kiểm bằng CODE:** cả nhánh glossary lẫn entity gate `runtime = provenance.branch==="runtime_memory" || span.provenance`; overlay span mang `provenance:"runtime_memory"` → render "Runtime term/entity" (forms_used/scope/agent-built/surface), field gold (allowed/forbidden/annotator/confidence) CHỈ ở nhánh non-runtime (gold_demo, quarantined). Không cần hover tay.
  - **Perf ✓ (mình đo trên d2l_p1 THẬT):** block 0.047s, chapter 1.08s. Full-audit 16.9s nhưng UI gọi scoped (block/chapter) nên không chạm. Đúng hướng per-block/on-demand.
  - **Word-boundary ✓:** `(?<!\w)…(?!\w)` → "AI" không khớp trong "brain"; đa-từ khớp literal. **Security ✓:** `_db_path` validate JOB_ID_RE + chặn path-escape, `mode=ro`. **Determinism ✓:** sorted + non-overlap select ổn định.
  - **0-API / engine / scorer / schema dataset KHÔNG đổi ✓** (occurrences tính read-time). Read-model tách: matching (overlay/A01) ⊥ status/forms_used (D01 qua `load_scores`). Tự chạy: **3 overlay + 130 full** pass.
- **Notes (minor, fix-forward — KHÔNG chặn):**
  1. `_in_scope` tính evidence/block rồi `return True` vô điều kiện → scope thực tế chỉ theo CHƯƠNG (term tô ở mọi block trong chương có surface, không giới hạn evidence-block). Chấp nhận với overlay surface-match; nên bỏ nhánh evidence chết cho đỡ gây hiểu nhầm.
  2. NFC normalize áp lên needle nhưng KHÔNG lên text (để giữ offset) → rủi ro tiềm ẩn nếu text dịch là NFD; VI output thường NFC nên ổn. Ghi nhận.
  3. Số trên hover (`forms_used`) là count của SCORER (chuẩn); số span highlight là tra-vị-trí hiển thị, có thể lệch count — đúng GATE "char-span = display-only", không vi phạm.
  4. Chưa có **perf-budget test** (assert overlay scoped < ~Ns) → nên thêm để chặn regress (đúng lời khuyên mình đưa). Gộp follow-up.
- **Quyết định:** ACCEPT, commit cả cụm (overlay service+route+tests + frontend marks/hover/sidebar). "Trục D nhìn được" đã hiện thực: registry agent-xây sáng ở cả gốc + dịch, màu theo drift, card runtime.

---

**GATE:** Overlay = **observe-only, 0-API, deterministic dò-chuỗi**. Highlight memory **agent-xây** (KHÔNG gold). status/forms_used **ĐỌC từ scorer** (KHÔNG recompute); biến thể tô == `forms_used` (hình == số). Tô CẢ gốc + dịch, link `term_id`, màu theo drift. KHÔNG engine/scorer change, KHÔNG đổi schema dataset, gold giữ quarantine.
