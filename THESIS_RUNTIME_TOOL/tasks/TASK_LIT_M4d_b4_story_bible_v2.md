# TASK LIT M4d — B4 v2: Story Bible thật (thay pilot ch1)

> **Trạng thái:** DRAFT vòng-1 (Claude, 2026-07-11) — chờ CodeX critique (5.6 Sol xhigh),
> tối đa 2 vòng rồi mới implement. KHÔNG viết code trước khi chốt spec.
> **Bối cảnh:** GATE M4c chốt M2/gpt-5.4 PASS; B4-pilot FAIL 3/5 acceptance
> (xem TASK_LIT_M4c_full_run.md, mục GATE CUỐI). M3 hiện tại là code pilot ch1
> chạy lặp 4 lần — hết tầm đúng dự kiến. Chặn scale 34 chương cho tới khi B4 v2 pass.

## 0. Nguyên tắc khoá (không bàn lại)
- Code KHÔNG làm việc ngôn ngữ: mọi phán đoán identity/phase/xưng hô thuộc LLM;
  code chỉ cơ học tất định (dedup surface, sort, interval bookkeeping).
- Input của B4 = m1 checkpoints (as-of) + digests (đã chốt gpt-5.4) — KHÔNG chạy lại M1/M2.
- Style-free: B4 phát facts + observed vocatives; không quyết xưng hô VN
  (LITERARY_STYLE_PROFILE_V1 xử lý sau, tầng khác).
- Đo trước khi tin: mọi bước LLM mới phải có bộ acceptance case trên ch1-4 trước khi wire.

## 1. Thiết kế đề xuất (Claude, vòng-1)

### 1.1 Identity adjudication = LLM một call/scope, code chỉ ĐỀ XUẤT
- Code (cơ học): group ledger theo surface-core như hiện tại NHƯNG chỉ để tạo
  CANDIDATE cluster + gom evidence (aliases, quotes m1, identity facts từ digest —
  digest ch04 đã chứa "Mrs. Heathcliff = Catherine Linton", "Hareton = Earnshaw cuối").
- LLM (gpt-5.4, 1 call/scope, JSON): mỗi candidate cluster → verdict
  `merge | split | keep_separate | uncertain` + evidence_block per verdict.
  Bao gồm cả fragment (ent_catherine_s, ent_your_servant_zillah, ent_and_mrs_heathcliff)
  và cross-id join (ent_hareton ↔ ent_hareton_earnshaw).
- Code apply: chỉ apply verdict có evidence_block hợp lệ (block_id tồn tại);
  `uncertain` → giữ tách + flag needs_human_review. Mirror pattern Term-Auditor +
  3-gate của §29 (audit-label + no-new-collision).
- Canary bắt buộc: Mrs. Heathcliff KHÔNG được nằm chung entity với Heathcliff;
  Mr. Heathcliff PHẢI chung với Heathcliff; Hareton 2 id PHẢI join; King Lear giữ
  mentioned_historical.

### 1.2 As-of subsetting: bible-as-of-N từ chain checkpoint (máy móc M4b có sẵn)
- Mỗi file `wh_chNN_story_bible.json` = consolidation của ledger as-of N
  (đọc `checkpoints/m1/<=N`) + digests 1..N. KHÔNG đọc m1_report cuối.
- Lý do chọn per-chapter cumulative (thay vì 1 bible cuối + query as-of):
  Translator chương N+1 tiêu thụ đúng file N; cấu trúc file nhỏ, dễ verify;
  vẫn giữ interval để query trong-chương.
- scope/audit.scope = `M3_asof_chNN` (bỏ hardcode ch1); canary set theo scope (§1.4).

### 1.3 Interval + phase: chuẩn hoá pair, đóng interval bằng change-point từ digest
- Pair = tuple KHÔNG thứ tự, sort trước khi so — diệt duplicate
  (lockwood,heathcliff)/(heathcliff,lockwood).
- Nguồn phase = `candidate_transition` + `character_state_changes` của digest
  (LLM đã phán) — code chỉ dựng timeline: phase mới cùng pair → đóng interval cũ
  (`valid_to = trigger_block - 1` theo thứ tự block tuyệt đối).
- Valence fallback hiện tại GIỮ làm tầng cuối nhưng phải dán nhãn
  `phase_source=fallback` như hiện nay và không đè digest-driven phase.

### 1.4 Canary per-scope (acceptance của chính task này)
- as-of ch1: Hareton mentioned_historical (inscription); KHÔNG có Hindley/Zillah/
  Catherine Linton/Nelly trong registry; Heathcliff merge đúng; King Lear chưa tồn tại.
- as-of ch2: Hareton on-stage + join 2 id; daughter-in-law phase tồn tại cho pair
  (heathcliff, mrs_heathcliff/catherine_linton) với evidence b042/b046; King Lear
  mentioned_historical.
- as-of ch3: ghost/diary identity facts có mặt; vẫn không leak entity ch4 (Nelly
  narration ch4). as-of ch4: narrator switch 2 segments; identity facts
  (Mrs. Heathcliff = Catherine Linton; Hareton = Earnshaw cuối) phản ánh trong registry.
- entity_type: không hardcode person — Wuthering Heights/Thrushcross Grange về place
  (nguồn: glossary category của M1, cơ học).

### 1.5 Chi phí
- +1 LLM call identity-adjudication per as-of scope (4 call cho ch1-4, gpt-5.4, ước
  <20k tok tổng — trong quota). 0-API cho phần còn lại. Scale 34 chương: 34 call nhỏ.

## 2. Câu hỏi mở đánh số (CodeX trả lời từng câu, kèm trích code nếu claim)
1. Adjudication 1 call/scope hay 1 call/book-rồi-project-as-of? (per-scope tránh
   future leak trong chính lời phán; per-book rẻ hơn 8x khi scale. Claude nghiêng
   per-scope-cumulative: call as-of N chỉ thấy evidence <=N.)
2. Schema verdict JSON tối thiểu cần field gì để code apply tất định không suy diễn?
3. Đóng interval `valid_to = block trước trigger` có ổn với block numbering per-chapter
   (wh_ch02_b001 sau wh_ch01_b092)? Cần thứ tự block tuyệt đối toàn sách ở đâu?
4. Có case nào code apply verdict LLM tạo collision mới (2 cluster merge vào 1 id
   đã tồn tại)? Cần gate no-new-collision như §29?
5. Fragment garbled (ent_and_mrs_heathcliff) nên `split` về đâu khi surface không
   parse được — quarantine như §30 review_only?

## 3. Điều kiện dừng
- Sau vòng-2: Claude chốt spec cuối, CodeX implement + dry-run 0-API trên ch1-4
  (adjudication call render prompt thật, --confirm trước khi gọi API).
- Gate: Claude tự chấm §1.4 trên artifact thật. PASS → mở đường scale 34 chương.
