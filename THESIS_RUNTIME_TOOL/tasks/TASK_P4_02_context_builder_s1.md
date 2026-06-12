# TASK_P4_02_context_builder_s1 — Context Builder (anchor → hard constraints) + chạy S1 + số S0/S1/oracle

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §2.1 (code modules: Query Planner / Hybrid
  Retriever / Reranker+Budget / Coverage Checker; Deterministic Context Feeding),
  §5 (quy tắc anchor-based: "chỉ nhét thứ match anchor trong window, KHÔNG BAO GIỜ
  dump cả registry"; Zone 3 hard ≤ ~500 tok), §5.2 (hard đè soft; hard ≠ verbatim;
  PROMPT_DESIGN §1.6 consistency ≠ verbatim), changelog (u) (window là đơn vị);
  windower P3-01 ĐÃ đảm bảo 1 window = 1 state xưng hô (cắt tại trigger đổi pha);
  thước đo P3-01 (`thesis_scoring` + same ruler)
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

Nấc S1 của thang ablation: S0 + **hard constraints từ memory frozen** (glossary T1 +
entity card T2 + xưng hô active từ entity_relations). Đây là con số trả lời câu hỏi
trung tâm của khóa luận lần đầu tiên: *bơm memory vào thì TAR nhảy bao nhiêu so với
0.4151, và có vượt oracle 0.6226 không?* (Kỳ vọng: VƯỢT — oracle chỉ tự xây glossary
khi dịch, S1 được bơm thẳng registry frozen.) Chroma CHƯA dùng ở nấc này (soft track
là S3 — P4-04); retriever S1 = keyed-lookup SQLite thuần.

## 2. Scope

**IN:**
1. `pipeline/retrieval/context_builder.py` — code tất định, 0 token LLM:
   - `plan_anchors(conn, window_blocks) -> Anchors`: quét text các block thành viên
     (matching word-boundary + NFC + IGNORECASE + apostrophe-normalize — REUSE helper
     của `span_resolver`/`thesis_scoring`, không viết triết lý matching thứ 3):
     - term anchors: glossary_entries có source_term match trong block nào → map
       term → [block_ids].
     - entity anchors: entities có canonical/alias match → entity → [block_ids].
     - `has_dialogue`: window chứa block_type='dialogue'.
   - `build_context_pack(conn, window, anchors, budget_tokens=500) -> ContextPack`:
     - **Glossary lines** (chỉ term match): `source_term → target_term` (+ ` [GIỮ
       NGUYÊN]` nếu do_not_translate).
     - **Entity cards** (chỉ entity match): `canonical_source → canonical_target
       (aliases_vi)` — 1 dòng/entity.
     - **Address policy**: với mỗi CẶP entity cùng match trong window, tra
       `entity_relations` state ACTIVE tại order_index của block đầu window
       (valid_from ≤ idx ≤ valid_to hoặc valid_to NULL) → dòng
       `A→B: "<address_a_to_b>", B→A: "<address_b_to_a>" (state)`. Windower đã đảm
       bảo 1 window 1 state — KHÔNG cần xử lý đa-state trong window, assert nếu gặp.
     - **Budget + Reranker**: ước token chars/4; vượt `budget_tokens` → drop theo
       priority: address policy GIỮ TRƯỚC HẾT > entity cards > glossary theo số
       occurrence trong window giảm dần; mọi mục bị drop ghi vào
       `dropped_by_budget` (list) — số liệu chẩn đoán S3d sau này.
     - **Coverage Checker** (van xả #1): sau khi đóng pack, mỗi term/entity anchor
       phải có dòng tương ứng (trừ thứ nằm trong dropped_by_budget) → thiếu:
       re-build 1 lần; vẫn thiếu → flag `low_context=true` vào pack. 0 token.
   - `ContextPack`: {lines render sẵn, token_estimate, anchors, dropped_by_budget,
     low_context} — serialize được vào memory_packs.payload_json.
2. `pipeline/translate/prompt.py` — mở rộng (KHÔNG phá S0):
   - `build_messages(window_blocks, config, context_pack=None, prompt_version=...)`.
   - S0: như cũ, purity giữ nguyên (test cũ phải pass nguyên vẹn).
   - S1 (`prompt_version="s1_v1"`): user message thêm section
     `MANDATORY TERMINOLOGY & NAMES` (glossary + entity lines) +
     `ADDRESS POLICY (xưng hô)` TRƯỚC phần source. Diễn đạt hard-đè-soft đúng §1.6:
     "dùng ĐÚNG dạng đích đã cho mỗi khi khái niệm/tên xuất hiện (mọi biến thể cú
     pháp tiếng Việt hợp lệ quanh nó đều được); thuật ngữ [GIỮ NGUYÊN] không dịch;
     xưng hô giữa các nhân vật phải theo policy".
3. `pipeline/translate/runner.py` — mở rộng: nhận `context_builder` hook; mỗi window
   build pack → messages theo config; persist memory_packs.payload_json =
   {window_id, block_ids, zones: {system_tokens, hard_constraints_tokens,
   source_tokens}, prompt_version, anchors_count, dropped_by_budget, low_context} —
   đây chính là `token_breakdown` LOCK §5 yêu cầu. translation_runs config='S1'
   (UNIQUE theo (experiment, block, config, stage) — chạy chung experiment
   `exp_pilot_p3` với S0 là ĐÚNG Ý ĐỒ để so trong cùng experiment).
4. `pipeline/scripts/run_translate.py` — nhận `--config S1` (mặc định vẫn S0).
5. Score: `score_run` chạy với `--config S1` → `data/reports/s1_pilot_consistency.json`
   (tracked; cấu trúc như report S0: {"ruler", "s1", "oracle_same_ruler"}).
6. Tests offline `pipeline/tests/test_context_builder.py` (+ cập nhật tối thiểu
   test_translate_runner cho hook): fixture DB nhỏ tự tạo (KHÔNG đụng DB frozen).

**OUT:** Chroma/vector trong context (P4-04); rolling window + chapter summary Zone 2
(P4-03); Brief + Critic + Repair (P4-04); S2/S3; re-run S0 (đã có, không đụng);
UI. KHÔNG ghi gì vào 5 bảng memory frozen; KHÔNG sửa `eval/consistency.py`.

## 3. Spec — chi tiết chốt

- Active state lookup theo `blocks.order_index`: state có `order_index(valid_from) ≤
  idx_window_start` và (`valid_to` NULL hoặc `idx_window_start ≤ order_index(valid_to)`).
  Nhiều state thỏa (không nên xảy ra sau windower) → lấy valid_from lớn nhất + ghi
  warning vào pack.
- Pack render template CỐ ĐỊNH 1 dòng/mục (COMPACT rule #1 LOCK §5.1) — không văn xuôi.
- Sanity bắt buộc in cuối run thật: số window có low_context, tổng dropped_by_budget
  (kỳ vọng pilot: cả hai = 0 vì registry nhỏ).
- Bảng so sánh §5 PHẢI gồm 4 cột: S0 / S1 / oracle / Δ(S1−S0), và 3 block mẫu
  PHẢI có `treasure_island_ch02_b005` (bài hát — "ngực người chết" của S0 có được
  hard constraint "the Dead Man's Chest → Rương Người Chết" cứu không?) + 1 block
  rum + 1 block thoại có xưng hô theo policy.

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_context_builder.py pipeline/tests/test_translate_runner.py -v
# PHẢI PASS (fixture DB tự tạo, không mạng):
# 1. test_anchor_scan: term + entity match đúng block (word-boundary: "rum" không
#    match "rumor"; apostrophe ’/' match nhau); has_dialogue đúng
# 2. test_no_registry_dump: registry 20 term nhưng window chỉ match 3 → pack chỉ
#    chứa 3 dòng glossary (quy tắc anchor-based §5)
# 3. test_address_policy_active_state: 2 state khác cửa sổ thời gian → window lấy
#    đúng state active theo order_index block đầu window
# 4. test_budget_drop_priority: budget nhỏ → glossary ít occurrence bị drop trước,
#    address policy giữ lại cuối cùng; dropped_by_budget ghi đủ
# 5. test_coverage_checker_flags: anchor có term mà pack thiếu dòng (mô phỏng) →
#    re-build 1 lần → vẫn thiếu → low_context=true
# 6. test_prompt_s1_contains_constraints_s0_unchanged: S1 messages chứa
#    MANDATORY TERMINOLOGY + ADDRESS POLICY; S0 messages KHÔNG đổi so với P3-01
# 7. test_runner_persists_pack_breakdown: payload_json đủ zones token breakdown +
#    anchors_count + low_context; translation_runs config='S1'

# Chạy thật S1 (~31 call, ước ~35-45k prompt tokens, ~$0.03; dán console vào §5):
python -m pipeline.scripts.run_translate --db data/jobs/treasure_island_p2/memory.sqlite3 --chapters ch02 ch03 --config S1 --experiment exp_pilot_p3 --source data/sources/treasure_island/document.json
# - exit 0; 81/81 blocks; json_fail < 5%; in low_context count + dropped count
# - chạy lại → skipped 100%, 0 token

python -m pipeline.scripts.score_run --db data/jobs/treasure_island_p2/memory.sqlite3 --experiment exp_pilot_p3 --config S1 --prepass data/prepass/treasure_island_pilot --source data/sources/treasure_island/document.json --oracle "../AILAB_HANDOFF/ailab_projects/treasure_island" --out data/reports/s1_pilot_consistency.json
# - report tracked; §5 điền bảng S0/S1/oracle/Δ + 3 block mẫu THẬT từ DB
#   (bắt buộc có b005 dead man's chest; 1 block rum; 1 block thoại xưng hô)

python -m pytest pipeline/tests/ -v   # toàn bộ vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

—

## 6. Review *(Claude điền)*

—
