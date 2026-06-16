# TASK_APP_A02_registry_overlay — Runtime registry overlay (highlight memory agent-xây ở CẢ gốc + dịch, tô theo drift)

- **Status:** READY
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

## 5. Implementation notes *(imple điền)*

*(điền: nơi tính occurrences (A01 vs composer) · thuật toán match source/target + normalize + longest-match/overlap · nguồn status/forms_used + cách join D01 · viewer wiring vào buildSpans + màu status + link term_id + runtime hover card + icon cột trái · test + browser smoke output)*

## 6. Review *(Claude điền)*

- **Verdict:** (trống)
- Findings: …
- Follow-up: …

---

**GATE:** Overlay = **observe-only, 0-API, deterministic dò-chuỗi**. Highlight memory **agent-xây** (KHÔNG gold). status/forms_used **ĐỌC từ scorer** (KHÔNG recompute); biến thể tô == `forms_used` (hình == số). Tô CẢ gốc + dịch, link `term_id`, màu theo drift. KHÔNG engine/scorer change, KHÔNG đổi schema dataset, gold giữ quarantine.
