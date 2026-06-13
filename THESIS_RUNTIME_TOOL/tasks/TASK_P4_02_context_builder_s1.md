# TASK_P4_02_context_builder_s1 — Context Builder (anchor → hard constraints) + chạy S1 + số S0/S1/oracle

- **Status:** DONE
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

### 5.1. Files changed

- `pipeline/retrieval/context_builder.py`
  - Added deterministic `plan_anchors` + `build_context_pack`.
  - Anchor matching is NFC + ignorecase + apostrophe-normalized + word-boundary.
  - Entity matching de-duplicates overlapping aliases by longest span, so `the captain`
    and `captain` do not double-count the same mention.
  - Context pack only includes anchors matched inside the current window; no registry dump.
  - Active address policy is selected from `entity_relations` by `blocks.order_index` at
    window start. Multiple active candidates are resolved by latest `valid_from` and logged
    as warning.
  - Budget order: address policies kept first, then entity cards, then glossary by occurrence
    count. Dropped items go into `dropped_by_budget`.
  - Coverage checker rebuilds once; unresolved missing anchor sets `low_context=true`.
- `pipeline/translate/prompt.py`
  - `build_messages(..., config="S0"|"S1", context_pack=None)` is backward-compatible:
    S0 default output remains unchanged.
  - S1 uses prompt version `s1_v1` and prepends `MANDATORY TERMINOLOGY & NAMES`,
    `ADDRESS POLICY (xung ho)`, then `SOURCE WINDOW`.
- `pipeline/translate/runner.py`
  - Added S1 context-builder hook.
  - Persists `memory_packs.payload_json` with `zones`, `prompt_version`, `anchors_count`,
    `dropped_by_budget`, `low_context`, and full `context_pack`.
  - `translation_runs.prompt_version` is now `s1_v1` for S1.
- `pipeline/scripts/run_translate.py`
  - `--config` accepts `S0|S1`; prints context sanity summary for S1.
- `pipeline/scripts/score_run.py`
  - Report key and console label now follow requested config (`s0`, `s1`, ...), not hardcoded
    S0.
- `pipeline/tests/test_context_builder.py`
  - Offline tests for anchor scan, no registry dump, active address state, budget drop
    priority, coverage checker, and S1 prompt/S0 unchanged.
- `pipeline/tests/test_translate_runner.py`
  - Added S1 pack-breakdown persistence test.
- `data/reports/s1_pilot_consistency.json`
  - Tracked S1 consistency report on the same ruler as S0/oracle.

No changes to `app/`, `AILAB_HANDOFF/`, or `eval/consistency.py`.

### 5.2. Test output

```text
python -m pytest pipeline/tests/test_context_builder.py pipeline/tests/test_translate_runner.py -v

collected 14 items
14 passed in 14.96s
```

```text
python -m pytest pipeline/tests/ -v

collected 75 items
75 passed in 49.15s
```

Windows still prints the known pytest temp cleanup warning after success:
`PermissionError: [WinError 5] Access is denied: 'D:\temp\pytest-of-Snail\pytest-current'`.
Exit code is still 0.

### 5.3. Real S1 run

First run:

```text
python -m pipeline.scripts.run_translate --db data/jobs/treasure_island_p2/memory.sqlite3 --chapters ch02 ch03 --config S1 --experiment exp_pilot_p3 --source data/sources/treasure_island/document.json

Windows total:     31
  translated:      31
  failed:         0
  skipped:        0
Blocks translated: 81
Blocks failed:    0
JSON fail rate:   0.0000

prompt_tokens:      21457
completion_tokens:  8393
total_cost_usd:    $0.022150
incremental_cost:   $0.022150
calls:             31
cache_hits:        0
Model: gpt-5.4-mini  Seed: 20260612

windows_with_context: 31
low_context_windows:  0
dropped_by_budget:    0
```

Resume run:

```text
Windows total:     31
  translated:      0
  failed:         0
  skipped:        31
Blocks translated: 0
Blocks failed:    0
JSON fail rate:   0.0000

prompt_tokens:      0
completion_tokens:  0
total_cost_usd:    $0.000000
incremental_cost:   $0.000000
calls:             0
cache_hits:        0
```

DB aggregate check:

```text
runs_by_config [('S0', 81), ('S1', 81)]
s1_packs 31
low_context 0
dropped_by_budget 0
```

### 5.4. Score output

```text
python -m pipeline.scripts.score_run --db data/jobs/treasure_island_p2/memory.sqlite3 --experiment exp_pilot_p3 --config S1 --prepass data/prepass/treasure_island_pilot --source data/sources/treasure_island/document.json --oracle "../AILAB_HANDOFF/ailab_projects/treasure_island" --out data/reports/s1_pilot_consistency.json

=== S1 ===
  TAR overall:    1.0000  (53 pairs)
  FVR overall:   0.0000
  ECS overall:   0.8111
    ch02: 1.0000
    ch03: 1.0000

=== Oracle (same ruler) ===
  TAR overall:    0.6226  (53 pairs)
  FVR overall:   0.0000
  ECS overall:   0.7667
    ch02: 0.6176
    ch03: 0.6316
```

Same-ruler comparison:

| Metric | S0 | S1 | Oracle | Delta S1-S0 |
|---|---:|---:|---:|---:|
| TAR overall | 0.4151 | 1.0000 | 0.6226 | +0.5849 |
| TAR occurrence-weighted | 0.3860 | 1.0000 | 0.6316 | +0.6140 |
| TAR ch02 | 0.3824 | 1.0000 | 0.6176 | +0.6176 |
| TAR ch03 | 0.4737 | 1.0000 | 0.6316 | +0.5263 |
| ECS overall | 0.7556 | 0.8111 | 0.7667 | +0.0556 |
| FVR overall | 0.0000 | 0.0000 | 0.0000 | +0.0000 |

Interpretation: S1 hard constraints fully cover the thesis registry's 53 TAR pairs on this
pilot. This is expected to be easier than oracle on TAR because oracle did not receive the
frozen glossary; it is not a claim of overall translation quality.

### 5.5. Real sample blocks

#### `treasure_island_ch02_b005` — Dead Man's Chest

Source:

```text
“Fifteen men on the dead man’s chest, Yo-ho-ho and a bottle of rum!”
```

S0:

```text
“Mười lăm người trên ngực người chết, Yo-ho-ho và một chai rum!”
```

S1:

```text
“Mười lăm người trên Rương Người Chết, Yo-ho-ho và một chai rượu rum!”
```

Oracle:

```text
“Mười lăm người trên rương người chết, Yo-ho-ho và một chai rượu rum!”
```

Pack evidence for this window:

```json
{
  "anchors_count": {"terms": 8, "entities": 4, "address_policies": 0},
  "dropped_by_budget": [],
  "low_context": false,
  "glossary_lines": [
    "saber cut -> vết chém kiếm cong",
    "Admiral Benbow Inn -> quán trọ Admiral Benbow",
    "cove -> vịnh nhỏ",
    "handbarrow -> xe cút kít tay",
    "rum -> rượu rum",
    "sea-chest -> rương biển",
    "the Dead Man's Chest -> Rương Người Chết",
    "Treasure Island -> Đảo Châu Báu"
  ]
}
```

#### `treasure_island_ch02_b006` — rum

Source:

```text
in the high, old tottering voice that seemed to have been tuned and broken at the capstan bars. Then he rapped on the door with a bit of stick like a handspike that he carried, and when my father appeared, called roughly for a glass of rum. This, when it was brought to him, he drank slowly, like a connoisseur, lingering on the taste, and still looking about him at the cliffs and up at our signboard.
```

S0:

```text
bằng giọng cao, già nua, run rẩy, nghe như đã được lên dây và làm vỡ ở những thanh quay tời. Rồi ông ta gõ cửa bằng một mẩu gậy như một cây xà beng mà ông mang theo, và khi cha tôi xuất hiện, ông ta thô lỗ gọi một cốc rượu rum. Khi được mang ra, ông ta uống chậm rãi, như một tay sành sỏi, nấn ná thưởng vị, mà vẫn nhìn quanh những vách đá và ngước lên tấm biển hiệu của chúng tôi.
```

S1:

```text
bằng giọng cao, cũ kỹ, run rẩy như thể đã được lên dây và làm hỏng ở những thanh quay tời. Rồi hắn gõ cửa bằng một đoạn gậy trông như một cây đòn bẩy mà hắn mang theo, và khi cha tôi xuất hiện, hắn thô lỗ gọi một cốc rượu rum. Khi được mang ra cho hắn, hắn uống chậm rãi, như một tay sành sỏi, nấn ná thưởng thức vị, và vẫn nhìn quanh những vách đá rồi ngước lên tấm biển hiệu của chúng tôi.
```

Oracle:

```text
bằng cái giọng cao, già nua, lảo đảo, tưởng như từng được lên dây rồi bị bẻ gãy bên những thanh quay tời neo. Rồi lão gõ cửa bằng một mẩu gậy mang theo, giống như đòn bẩy tàu, và khi cha tôi xuất hiện, lão thô lỗ gọi một ly rượu rum. Khi ly rượu được mang ra, lão uống chậm rãi như kẻ sành sỏi, nấn ná tận hưởng vị rượu, mắt vẫn nhìn quanh các vách đá rồi ngước lên tấm biển hiệu của chúng tôi.
```

#### `treasure_island_ch02_b022` — dialogue/address policy

Source:

```text
“Were you addressing me, sir?” said the doctor; and when the ruffian had told him, with another oath, that this was so, replied, “I have only one thing to say to you, sir, that if you keep on drinking rum, the world will soon be quit of a very dirty scoundrel!”
```

S0:

```text
“Ông đang nói với tôi đấy à, thưa ông?” vị bác sĩ nói; và khi tên du côn kia, kèm thêm một lời thề độc, bảo ông rằng đúng là như vậy, ông đáp: “Tôi chỉ có một điều muốn nói với ông, thưa ông: nếu ông cứ tiếp tục uống rượu rum, chẳng bao lâu nữa thế gian này sẽ bớt đi một tên khốn kiếp rất bẩn thỉu!”
```

S1:

```text
“Ông đang nói với tôi đấy à, thưa ông?” bác sĩ nói; và khi tên côn đồ đã nói với ông, kèm thêm một lời thề thốt khác, rằng đúng là như vậy, ông đáp: “Tôi chỉ có một điều muốn nói với ông, thưa ông, rằng nếu ông cứ tiếp tục uống rượu rum, chẳng bao lâu nữa thế gian này sẽ bớt đi một tên khốn bẩn thỉu!”
```

Oracle:

```text
“Có phải ông đang nói với tôi không, thưa ông?” bác sĩ hỏi; và khi tên côn đồ ấy, kèm một câu chửi khác, cho biết đúng là vậy, ông đáp: “Tôi chỉ có một điều để nói với ông, thưa ông: nếu ông cứ tiếp tục uống rượu rum, chẳng bao lâu thế gian này sẽ thoát được một tên vô lại bẩn thỉu!”
```

Pack evidence:

```json
{
  "anchors_count": {"terms": 1, "entities": 2, "address_policies": 1},
  "dropped_by_budget": [],
  "low_context": false,
  "address_lines": [
    "Billy Bones->Doctor Livesey: \"ông\", Doctor Livesey->Billy Bones: \"ông\" (open_confrontation)"
  ]
}
```

## 6. Review *(Claude điền — 2026-06-13)*

- **Verdict: PASS.** Số liệu đã tái tính độc lập (quy tắc sau vụ agent ngoài): tự chạy
  75/75 tests; tự đọc report → **S1 TAR 1.0 / S0 0.4151 / oracle 0.6226 trên CÙNG 53
  pairs**; tự kéo b005/b011/b033 từ DB khớp report; S0 report không bị đụng (vẫn
  0.4151). Code context_builder đúng spec: anchor-keyed (chỉ query term/entity match
  trong window — `WHERE glossary_id IN (...)`, không dump registry), budget ưu tiên
  address-policy giữ trước, coverage re-build 1 lần rồi mới `low_context`. low_context=0,
  dropped=0 (registry nhỏ — đúng kỳ vọng). Hard constraints trung bình 58,9 tok/window
  (≤ budget 500, dư địa lớn). Không bịa, không giả danh reviewer.
- **Bằng chứng demo đắt nhất ĐÃ về:** b005 "ngực người chết" (S0, sai nghĩa) →
  **"Rương Người Chết"** (S1) nhờ hard constraint `the Dead Man's Chest → Rương Người
  Chết` — pack evidence chứng minh đúng dòng được bơm. Đây là slide trung tâm cho Thầy.
- **PHÁT HIỆN PHƯƠNG PHÁP LUẬN QUAN TRỌNG (đã khóa LOCK changelog (z)): TAR BÃO HÒA
  ở S1.** TAR=1.0 KHÔNG phải "S1 dịch hoàn hảo" mà là hệ quả cấu trúc: provider
  (span_resolver) chấm đúng tập term có source xuất hiện trong window, mà context
  builder bơm thẳng target đã duyệt cho CHÍNH tập đó → model tuân lệnh → 1.0 gần như
  tất yếu một khi injection chạy. Hệ quả cho luận văn:
  1. TAR là thước TUYỆT VỜI để chứng minh "memory injection ăn tiền" (Δ +0.585 vs S0,
     vượt oracle) — nhưng nó **không phân biệt được S1 vs S2 vs S3** (đều sẽ ~1.0).
  2. Phân hóa S1→S3 PHẢI đo bằng trục khác: **ECS** (mới 0.8111, còn dư địa — xưng hô
     động + narrator artifact), **semantic quality** (judge/COMET — pha eval sau), và
     **case study arc xưng hô** Jim↔Silver (cần chương dài hơn).
  3. Chống câu hỏi hội đồng "S1 đã TAR=1.0 thì cần S3 làm gì?": câu trả lời nằm ở
     những thứ hard constraint KHÔNG vá được — mạch văn, xưng hô theo pha, trung
     thành ngữ nghĩa. b022 minh chứng: address policy captain↔doctor = "ông"/"ông"
     nên S1 ≈ S0 ở thoại này — hard constraint vô hại nhưng cũng vô tác dụng khi
     quan hệ không đổi đại từ; giá trị thật của address policy chỉ lộ ở cặp có
     ĐỔI pha (Jim↔Silver, ngoài phạm vi 2 chương pilot).
- **Caveat trung thực phải mang theo:** TAR=1.0 là TRẦN của pilot 53 pairs / 2 chương /
  registry nhỏ / dropped=0. Ở quy mô sách (budget ép, term nhiều, dropped>0) TAR sẽ
  tụt dưới 1.0 — KHÔNG quảng cáo 1.0 như số vĩnh viễn.
- Findings nhỏ (không chặn): (1) `[GIU NGUYEN]` viết không dấu trong prompt — nhất
  quán, vô hại; (2) ECS ch02/ch03 = 1.0 từng chương nhưng overall 0.8111 do trung bình
  có trọng số gồm entity narrator/Doctor coverage thấp kéo xuống — đúng công thức EV-01,
  không phải lỗi.
- **P4-02 xong → P4-03 (Zone 2: rolling window + chapter summary → S2).** Lưu ý chiến
  lược: vì TAR đã bão hòa, từ P4-03 trở đi headline metric chuyển sang ECS + chuẩn bị
  hạ tầng judge; cân nhắc kéo một phần eval đa trục (P5) lên sớm để S2/S3 có thước phân
  biệt — sẽ bàn khi viết spec P4-03.
