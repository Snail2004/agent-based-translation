# TASK_P2_D2L_world_builder — World Builder D2L (chế độ thuật ngữ kỹ thuật) + chẩn đoán C (Builder-vs-gold) + FREEZE

- **Status:** READY
- **Refs:** LOCK (ee) (2 lớp claim; 3 loại knob; **Builder prompt D2L = chế độ thuật ngữ
  kỹ thuật**; phương pháp dev/test: tune trên dev-chapter RỜI HẲN benchmark → khóa config →
  chạy), (dd) (4 thước; C = Builder-vs-gold UNIQUE-term recall/agreement/**conflict-list**;
  allowed_variants), (cc) (gold EVAL-ONLY, CẤM bơm), Directional Lock (Builder học từ text
  EN trắng, KHÔNG annotation người), §6.3; engine có sẵn = World Builder TI (P2-01/P2-02:
  `pipeline/prepass/` + span_resolver), DB job = `data/jobs/d2l_p1/memory.sqlite3` (P1-02
  đã nạp 8803 blocks + gold 458, `glossary_entries` đang RỖNG)
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

P1-02 đã nạp EN D2L → blocks + gold eval-only. Giờ dựng **registry bơm** (`glossary_entries`)
bằng World Builder **tự học từ text EN** (Directional Lock) — KHÁC TI ở chỗ D2L là **thuật
ngữ kỹ thuật**, không phải nhân vật/quan hệ/motif. Đây là ô "Builder" tạo ra registry mà cả
ba thước A/B/C dựa vào.

Mục tiêu kép: (a) registry chất lượng cho thuật ngữ kỹ thuật; (b) **biết Builder tốt đến đâu
TRƯỚC khi tốn token dịch** → ra **chẩn đoán C (Builder-vs-gold)** ngay sau build. Theo phương
pháp dev/test (LOCK ee): tune prompt trên **dev chapter rời benchmark** → KHÓA → chạy benchmark.

**KHÔNG dịch ở task này** (S0/S1 + A/B/D + judge = P3-D2L). **Gold chỉ để chẩn đoán C, CẤM
bơm vào Builder/Translator** (guard như P1-02).

## 2. Scope

**Chương (tham số — chốt mặc định, dễ chỉnh):**
- **DEV** (tune + khóa): `deep_learning_computation` (rời bộ benchmark).
- **BENCHMARK** (chạy sau khi khóa): `introduction, preliminaries, linear_networks,
  multilayer_perceptrons` (4 liên tiếp; gồm 2 chương có "agent").

**IN:**
1. **Builder prompt chế độ THUẬT NGỮ KỸ THUẬT** (`pipeline/prepass/` — thêm prompt variant
   chọn-được qua config, KHÔNG xóa prompt TI). Trích từ text EN mỗi term:
   - `termhood` (có phải thuật ngữ đáng ghi không — loại từ thường);
   - `canonical_source` (EN) + `canonical_target` (VI — Builder cam kết MỘT dạng chuẩn);
   - `term_type` ∈ {term, abbreviation, proper_noun, code_api};
   - `do_not_translate` (tên framework/library/API/code, vd "PyTorch", "softmax" nếu giữ);
   - `allowed_variants` (các dạng VI khác Builder thấy hợp lệ) + `forbidden_variants`
     (dịch literal sai cần tránh, nếu suy ra được);
   - `evidence_span_ids` (block làm bằng chứng).
   Reuse khung T1–T4 + persist `glossary_entries` (và `entities` cho khái niệm nếu có).
2. **Tune trên DEV → KHÓA:** chạy Builder trên dev chapter, chỉnh prompt tới khi termhood/
   canonical hợp lý; ghi `prompt_version` cố định; §5 ghi rõ version + vài mẫu trích dev.
   **Sau khi khóa KHÔNG sửa prompt nữa.**
3. **Chạy Builder (prompt đã khóa) trên 4 chương BENCHMARK** → populate `glossary_entries`
   + `entities`. **CHỈ đọc `blocks.original_text` (EN); TUYỆT ĐỐI không đọc
   `eval_glossary_gold`** (test guard).
4. **Consolidation/Span Resolver:** gộp term trùng xuyên window/chương; **giải xung đột**
   (window A nói VI-x, window B nói VI-y → chọn canonical theo luật tất định: tần suất cao
   nhất → tie-break first-seen; ghi phần còn lại vào allowed_variants). Đây là **độ-sạch nội
   tại của registry** (ảnh hưởng D sau này).
5. **FREEZE** bảng memory sau build (migration 004 triggers) — registry bất biến trước benchmark.
6. **Chẩn đoán C** `data/reports/d2l_builder_vs_gold.json` (tracked), trên các gold term có
   EN xuất hiện trong 4 chương benchmark:
   - `recall` = (#gold term Builder bắt được) / (#gold term xuất hiện trong benchmark);
   - `agreement` = (#term TRÙNG mà canonical_target Builder ⊆ {gold target}) / (#term trùng)
     — matching CÓ chuẩn hóa (hoa/thường, dấu, khoảng trắng) để tránh xung đột giả;
   - `conflict_list` = danh sách [source_term, builder_target, gold_target] khi khác nhau
     (CHO NGƯỜI SOI — chưa phán sai, vì gold chưa có variants; vd agent/model/loss);
   - `extra_terms` = term Builder có mà gold không (BÁO RIÊNG, **KHÔNG tính lỗi** — gold chỉ
     phủ một phần, chương có thuật ngữ riêng).
7. Tests offline `pipeline/tests/test_d2l_builder.py` (fake transport): prompt parse đúng
   schema kỹ thuật; consolidation dedup + giải-xung-đột tất định; **guard Directional Lock**
   (Builder KHÔNG truy cập eval_glossary_gold trong lúc build); tính C đúng trên fixture
   (recall/agreement/conflict/extra); freeze chặn ghi sau build.

**OUT (P3-D2L):** dịch S0/S1; thước A/B/D + ECS; judge sample; **curate allowed_variants cho
GOLD** (eval-side, để gỡ conflict giả ở C/B). KHÔNG đụng artifact TI, `app/`, prompt TI.

## 3. Spec — chốt chi tiết

- **Directional Lock tuyệt đối:** Builder chỉ thấy EN text; gold là EVAL-ONLY (guard + test).
  Không có tri thức người-làm nào vào registry — registry phải là thứ Builder *tự suy ra*.
- **Dev/test (chống tune-theo-test):** prompt + mọi config tune trên dev → KHÓA (`prompt_version`
  ghi lại) → benchmark chạy version đã khóa. Báo cáo nêu rõ version dùng cho benchmark.
- **canonical_target là cam kết DUY NHẤT** mỗi term (để bơm nhất quán); biến thể thấy thêm →
  `allowed_variants`, KHÔNG làm canonical dao động.
- **C là CHẨN ĐOÁN, không headline** (headline B/D ở P3-D2L). C chạy *trước* dịch để **gác
  chi tiêu**: nếu recall/agreement thảm → sửa prompt Builder (trên dev) trước khi đốt token dịch.
- **conflict_list ≠ lỗi:** gold chưa có variants nên nhiều "xung đột" là biến thể hợp lệ;
  liệt kê để người soi, sẽ lọc khi P3-D2L curate gold variants. Trung thực hơn binary fail.
- Cost: Builder pre-pass gpt-5.4-mini (effort low + temp 1.0 theo LOCK v), trong hạn 2.5M/ngày.

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_d2l_builder.py -v
# PHẢI PASS (offline, fake transport):
# - prompt kỹ thuật parse đủ field (termhood/canonical/type/do_not_translate/variants/evidence)
# - consolidation: gộp trùng + giải xung đột tất định (cùng input → cùng canonical)
# - GUARD Directional Lock: trong lúc build KHÔNG có truy vấn nào đọc eval_glossary_gold
# - tính C đúng trên fixture: recall/agreement/conflict_list/extra_terms
# - freeze: ghi vào memory sau build → raise

# Tune trên DEV (chỉnh prompt, KHÓA prompt_version) — §5 ghi version + mẫu:
python -m pipeline.scripts.run_prepass --db data/jobs/d2l_p1/memory.sqlite3 --chapters deep_learning_computation --mode d2l_terminology
# Chạy BENCHMARK (prompt đã khóa) → populate registry + freeze:
python -m pipeline.scripts.run_prepass --db data/jobs/d2l_p1/memory.sqlite3 --chapters introduction preliminaries linear_networks multilayer_perceptrons --mode d2l_terminology --freeze
# - glossary_entries CHUYỂN từ 0 → N (>0); entities nếu có; eval_glossary_gold KHÔNG đổi (458)

# Chẩn đoán C (trước khi dịch):
python -m pipeline.scripts.score_builder_vs_gold --db data/jobs/d2l_p1/memory.sqlite3 --chapters introduction preliminaries linear_networks multilayer_perceptrons --out data/reports/d2l_builder_vs_gold.json
# - in recall / agreement / số conflict / số extra; report tracked
# - "agent": Builder chọn gì vs gold "tác nhân" → có trong conflict_list nếu khác

python -m pytest pipeline/tests/ -v   # toàn bộ vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

—

## 6. Review *(Claude điền)*

—
