# TASK_P2_D2L_world_builder — World Builder D2L (chế độ thuật ngữ kỹ thuật) + chẩn đoán C (Builder-vs-gold) + FREEZE

- **Status:** DONE (REWORK→PASS — Claude 2026-06-14; xem §6.7)
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

### 5.0 Current result after registry-bloat rework

CodeX completed the agreed rework after Claude's §6.6 addendum:

- Copied the quota-failed v6/v6-like marker run to
  `data/_baseline/d2l_marker_v6_registry_bloat_20260614/` before rerun.
- Switched D2L extraction to locked prompt version `d2l_terminology_v7`.
- Removed `REGISTRY_SO_FAR` from D2L extraction windows. Each window now extracts independently;
  final consistency is handled by deterministic consolidation/persist.
- Fixed `PrepassRegistry.compress()` so glossary lines are actually capped when used by other modes.
- Added UTC daily quota accounting in the LLM client. OpenAI quota is counted by UTC day, not local day.
- Added preflight estimator and per-call prompt ceiling guard (`prompt_token_cap: 6000`).
- Used `OPENAI-KEY-2` for the rerun. This is a new separate account/key with fresh 2.5M token/day
  quota. The key value was not logged; `OPENAI-KEY-*.txt` is now gitignored.

Final status:

```text
DEV gate:
  prompt_version: d2l_terminology_v7
  calls: 28
  prompt_tokens: 38,298
  completion_tokens: 55,132
  json_fail_rate: 0.0
  C recall: 0.500000
  C agreement: 0.800000
  result: GO at the agreed threshold

4-chapter benchmark:
  chapters: introduction, preliminaries, linear_networks, multilayer_perceptrons
  prompt_version: d2l_terminology_v7
  calls: 227
  prompt_tokens: 309,829
  completion_tokens: 452,757
  total_tokens: 762,586
  json_fail_rate: 0.0
  memory_frozen: 1
  glossary_entries: 1,608
  eval_glossary_gold: 458 unchanged

Final C report:
  gold_terms_present: 163
  builder_terms: 1,608
  matched_terms: 121
  agreement_terms: 90
  recall: 0.742331
  agreement: 0.743802
  missing_terms: 42
  conflicts: 31
  extra_terms: 1,487
  report: data/reports/d2l_builder_vs_gold.json

Key term check:
  agent -> tác nhân
  intelligent agents -> tác nhân thông minh
  model -> mô hình
  loss function -> hàm mất mát
  softmax regression -> hồi quy softmax
```

Token-bloat fix evidence:

```text
Benchmark preflight:
  calls: 227
  prompt_tokens_est: 356,519
  max_prompt_tokens_est: 2,453
  total_tokens_upper_bound: 1,751,207
  prompt_token_cap: 6,000
  daily_token_cap: 2,400,000

Actual prompt tokens per call:
  introduction: min 1,039 / avg 1,252.7 / max 2,146 / n=48
  preliminaries: min 1,233 / avg 1,431.6 / max 1,695 / n=50
  linear_networks: min 1,113 / avg 1,383.8 / max 1,540 / n=51
  multilayer_perceptrons: min 1,100 / avg 1,378.8 / max 1,594 / n=78

OPENAI-KEY-2 cache usage for this rework:
  2026-06-13 UTC: 856,016 tokens / 255 calls
  = DEV 93,430 tokens + benchmark 762,586 tokens
```

Interpretation for review:

- The original C-gate failure is fixed: recall moved from 0.123 to 0.742331 on the 4-ch benchmark,
  and `agent -> tác nhân` is present in the frozen registry.
- The token growth failure is fixed for this path: benchmark prompt/call stays bounded around
  1.3k-1.4k and never approaches the 6k cap.
- This is ready for Claude review, not DONE. Claude should still inspect the 31 conflicts and the
  large extra-term list before approving PASS.

### 5.1 Files changed / added

- Added DB-backed prepass loader: `pipeline/prepass/db_source.py`.
  - Resolves spec slugs like `introduction`, `linear_networks` to real DB chapter ids
    `d2l_introduction`, `d2l_linear_networks`.
  - D2L World Builder reads heading + translate/prose blocks from `blocks.original_text`;
    code/math/image passthrough blocks stay out of the prompt to avoid token bloat.
- Added prompt mode `d2l_terminology` in `pipeline/prepass/prompt.py`.
  - Current locked prompt version after registry-bloat REWORK: `d2l_terminology_v7`.
  - D2L extraction deliberately omits registry context; consolidation handles consistency after
    window extraction.
  - D2L mode is glossary-only: entities/relations/mention_surfaces/motifs are normalized to `[]`.
  - Enforces concise technical terms, Vietnamese diacritics, compact evidence ids, and one canonical
    source entry per concept.
- Extended runner/schema/persist path:
  - `pipeline/prepass/runner.py`: DB-document runner, D2L pre-pass windowing, prompt version in report,
    glossary-only normalization, per-window valid block filtering.
  - `pipeline/prepass/schemas.py`: D2L fields `termhood`, `canonical_source`, `canonical_target`,
    `term_type`, variants, `evidence_span_ids`.
  - `pipeline/prepass/persist.py`: DB-memory build, deterministic conflict consolidation
    (most frequent target, tie = first-seen), unique glossary id generation, freeze support.
  - `pipeline/prepass/span_resolver.py`: resolves spans from in-memory DB-derived document.
- Added Builder-vs-gold scorer:
  - `pipeline/eval/builder_gold.py`
  - `pipeline/scripts/score_builder_vs_gold.py`
- Added offline tests: `pipeline/tests/test_d2l_builder.py`.

### 5.2 Important implementation notes / deviations

- `--out` is optional for DB prepass commands because the spec commands omit it.
  - Dev default: `data/prepass/d2l_dev`
  - Benchmark default: `data/prepass/d2l_benchmark`
- `--freeze` refuses to persist/freeze if any chapter failed. This prevents partial benchmark artifacts
  from freezing memory.
- REWORK replaced the old 1-call/chapter + cap-18 behavior with windowed pre-pass:
  - default target: ~500 source-token windows;
  - default max blocks/window: 16;
  - final chapter artifact merges all window outputs before persistence.
- D2L prepass includes headings as source context while still excluding code/math passthrough blocks.
- `missing_terms` was added to the C report in addition to recall/agreement/conflict/extra.
- Runtime reliability deviation from earlier LOCK/spec expectation:
  - `pipeline/configs/llm_prepass.yaml` now uses `reasoning_effort: "none"` and
    `max_output_tokens: 6144`.
  - Reason: `reasoning_effort: "low"` produced empty JSON responses where all completion budget was
    spent as reasoning tokens. A direct probe with `none` removed empty responses; increasing output
    budget avoided truncation on verbose windows.
  - Cache-key note: P0 replay cache key intentionally excludes `max_output_tokens`. This rework avoids
    stale cache reuse by changing prompt version and `reasoning_effort`, both already in the cache key.
    A future client task should decide whether `max_output_tokens` belongs in the cache key.

### 5.3 Commands run

```text
python -m pytest pipeline/tests/test_d2l_builder.py -v
9 passed

python -m pytest pipeline/tests/test_llm_client.py pipeline/tests/test_d2l_builder.py -v
17 passed in 4.95s

python -m pytest pipeline/tests/ -v
107 passed in 65.38s

python -m pipeline.scripts.run_prepass --db data/jobs/d2l_p1/memory.sqlite3 --chapters deep_learning_computation --mode d2l_terminology --cache data/jobs/prepass_cache_openai_key2.sqlite3 --preflight-only
calls: 28
prompt_tokens_est: 45,025
max_prompt_tokens_est: 1,693
total_tokens_upper_bound: 217,057

python -m pipeline.scripts.run_prepass --db data/jobs/d2l_p1/memory.sqlite3 --chapters deep_learning_computation --mode d2l_terminology --cache data/jobs/prepass_cache_openai_key2.sqlite3
prompt_version: d2l_terminology_v7
chapter: d2l_deep_learning_computation
status: passed
calls: 28
terms: 410
windows: 28
json_fail_rate: 0.0
reasoning_tokens: 0
prompt_tokens: 38,298
completion_tokens: 55,132
incremental_cost_usd: 0.1198385

DEV C-gate on a copied DB:
gold_terms_present: 40
builder_terms: 263
matched_terms: 20
agreement_terms: 16
recall: 0.5
agreement: 0.8
missing_terms: 20
conflicts: 4
extra_terms: 243

python -m pipeline.scripts.run_prepass --db data/jobs/d2l_p1/memory.sqlite3 --chapters introduction preliminaries linear_networks multilayer_perceptrons --mode d2l_terminology --cache data/jobs/prepass_cache_openai_key2.sqlite3 --preflight-only
calls: 227
prompt_tokens_est: 356,519
max_prompt_tokens_est: 2,453
total_tokens_upper_bound: 1,751,207

python -m pipeline.scripts.run_prepass --db data/jobs/d2l_p1/memory.sqlite3 --chapters introduction preliminaries linear_networks multilayer_perceptrons --mode d2l_terminology --cache data/jobs/prepass_cache_openai_key2.sqlite3 --freeze
prompt_version: d2l_terminology_v7
calls: 227
prompt_tokens: 309,829
completion_tokens: 452,757
json_fail_rate: 0.0
memory_frozen: 1
glossary_entries: 1,608

python -m pipeline.scripts.score_builder_vs_gold --db data/jobs/d2l_p1/memory.sqlite3 --chapters introduction preliminaries linear_networks multilayer_perceptrons --out data/reports/d2l_builder_vs_gold.json
gold_terms_present: 163
builder_terms: 1,608
matched_terms: 121
agreement_terms: 90
recall: 0.742331
agreement: 0.743802
missing_terms: 42
conflicts: 31
extra_terms: 1,487
```

Pytest on this Windows machine prints an ignored cleanup warning after pass:
`PermissionError: [WinError 5] Access is denied: 'D:\\temp\\pytest-of-Snail\\pytest-current'`.
It occurs after pytest reports success and did not change test status.

### 5.4 Benchmark status

Full benchmark completed on `OPENAI-KEY-2` with the new v7 extraction path and freeze enabled.

- `d2l_introduction`: 48 windows, 664 raw window terms, prompt avg 1,252.7, max 2,146.
- `d2l_preliminaries`: 50 windows, 664 raw window terms, prompt avg 1,431.6, max 1,695.
- `d2l_linear_networks`: 51 windows, 836 raw window terms, prompt avg 1,383.8, max 1,540.
- `d2l_multilayer_perceptrons`: 78 windows, 1,186 raw window terms, prompt avg 1,378.8, max 1,594.
- `data/prepass/d2l_benchmark/run_report.json` written.
- `data/reports/d2l_memory_build.json` written.
- `data/reports/d2l_builder_vs_gold.json` written.
- Main DB is frozen:
  - `glossary_entries=1608`
  - `memory_items=4`
  - `eval_glossary_gold=458`
  - `memory_frozen=1`

The earlier quota overrun was the daily TOKEN quota, not money: OpenAI CSV showed 2.735M tokens on
2026-06-13 UTC for the thesis project, costing only ~$0.2536. The `$4.96/$5.00` screenshot was an
UNRELATED project (gpt-5.5), NOT this thesis — do not read it as the thesis burning $5.

### 5.5 Final diagnostic evidence

Final 4-chapter C on the frozen benchmark DB:

```text
gold_terms_present: 163
builder_terms: 1608
matched_terms: 121
agreement_terms: 90
recall: 0.742331
agreement: 0.743802
missing_terms: 42
conflicts: 31
extra_terms: 1487
```

Important sanity checks from the frozen DB:

- `agent -> tác nhân` is captured.
- `intelligent agents -> tác nhân thông minh` is captured.
- `model -> mô hình` is captured.
- `linear regression -> hồi quy tuyến tính` is captured.
- `softmax regression -> hồi quy softmax` is captured.
- `loss function -> hàm mất mát` is captured.

Interpretation: REWORK fixed the original C-gate failure mode (`agent` missing and recall 0.123), and
the full 4-chapter benchmark now meets the agreed gate. The task is moved to REVIEW for Claude to inspect
the conflict/extra-term lists before PASS.

## 6. Review *(Claude điền)*

> Note for Claude re-review: the review below predates the final v7 rerun in §5.0. CodeX has now
> completed the full 4-chapter benchmark, frozen the DB, and produced final C. Please replace/update
> this section with the new verdict.

**Verdict: REWORK — KỸ THUẬT đạt, nhưng C-GATE = NO-GO (recall quá thấp).** (Claude, 2026-06-13)
Code đúng, trung thực, guard vững; nhưng chẩn đoán C — thứ ta CỐ Ý chạy trước khi dịch để
gác chi tiêu — báo registry chưa đủ làm nền cho P3-D2L. Phải vá Builder rồi qua lại C-gate.

### 6.1 Kỹ thuật — XUẤT SẮC (đã tái xác minh độc lập)
- Số khớp DB 100%: `glossary_entries` 0→**69**, `eval_glossary_gold` **458** (không đổi),
  `memory_frozen=1`. C: gold_present 163 / builder 69 / matched 20 / agreement 19 → **0.95**.
- **Directional Lock ĐẠT (loại mạnh):** build path KHÔNG query gold (chỉ `FROM blocks`);
  mention gold duy nhất là *câu CẤM* trong prompt; có test `test_build_memory..._does_not_read_gold`.
- **Tự bắt + sửa bug freeze-partial** (3/4 chương fail mà vẫn freeze → guard "chỉ freeze khi
  toàn pass"); **diacritics v3** (sửa target ASCII "kich thuoc lo"→có dấu); consolidation giải
  xung đột tất định; conflict DUY NHẤT là biến thể lành tính ("mã hóa one-hot" vs gold "biễu
  diễn one-hot" — gold còn có lỗi chính tả "biễu"). Không log key. **Trung thực mẫu mực**:
  từ chối tune prompt theo recall benchmark (giữ dev/test), phơi bày `agent` bị miss thay vì giấu.

### 6.2 C-GATE = NO-GO — đây là lý do REWORK (không phải lỗi code)
- **recall = 0.123** (Builder bắt 20/163 gold term hiện diện). **`agent`→`tác nhân` BỊ MISS.**
- **Gốc rễ:** runner chạy **1 LLM call/chương** + prompt ÉP **"at most 18 glossary candidates,
  prefer high-value over exhaustive coverage"**. Một chương MLP có 355 prose block / 108 gold
  term mà chỉ trích ≤18 trong 1 call → undersample nặng. Cap 18 là di sản prompt TI ("5-20
  terms/chapter"), KHÔNG hợp cho mục tiêu *nhất quán TOÀN BỘ thuật ngữ* của D2L.
- **Hệ quả nếu để nguyên:** S1 chỉ ép được 12% thuật ngữ → 88% còn lại Translator dịch tự do
  → **S1≈S0 trên B/D** → gap bị pha loãng, không chứng minh được kiến trúc + KHÔNG demo được
  chính từ `agent` (tâm điểm mối lo GVHD). → **C-gate đã làm đúng việc: chặn ta TRƯỚC khi đốt
  token dịch.**

### 6.3 Phạm vi REWORK (hợp lệ theo dev/test, LOCK ee — KHÔNG phải tune-theo-test)
1. **WINDOW hóa pre-pass Builder** (áp LOCK (u) như khâu dịch): thay 1-call/chương bằng nhiều
   window theo ngân sách token/chương, trích term mỗi window → **consolidate xuyên window+chương**
   bằng Span Resolver sẵn có. Để Builder THẤY HẾT block.
2. **Bỏ cap cứng 18** (window hóa xóa lý do phải cap để gọn prompt); GIỮ lọc termhood để không
   trích rác, nhưng KHÔNG giới hạn số lượng nhân tạo.
3. **Tinh chỉnh lại trên DEV** (deep_learning_computation) → đánh giá recall trên dev → **khóa
   prompt_version mới** → benchmark chạy 1 lần. **GO threshold đánh giá TRÊN DEV** (không trên
   benchmark): recall nâng rõ rệt (đề xuất ~≥0.5 trên gold-present của dev) + bắt được term tái
   xuất chính.
4. Giữ NGUYÊN mọi thứ tốt: guard Directional Lock, freeze-partial fix, diacritics, consolidation
   tất định, `missing_terms` trong report.

### 6.4 Nhận lỗi spec (của Claude, công bằng với CodeX)
TASK_P2_D2L mình viết **KHÔNG yêu cầu window hóa pre-pass + KHÔNG đặt sàn recall** → CodeX kế
thừa thiết kế 1-call/cap-18 của TI là ĐÚNG theo spec. Đây là **lỗ hổng spec của mình**, không
phải CodeX làm sai. Rework này là vá spec, không phải sửa lỗi ẩu.

### 6.5 Sau rework
Qua C-gate (recall đủ + agent bắt được) → mới PASS + commit (theo tiền lệ P3-01 REWORK→PASS).
Rồi mới sang P3-D2L (dịch + A/B/D + judge).

### 6.6 Addendum (2026-06-14) — 2 root-cause token-overrun (Claude xác minh độc lập)
Run rework vượt quota 2.5M/ngày (2.74M, +9%) — KHÔNG phải tiền (thesis project chỉ \$0.25),
mà là rate-limit token/ngày. Hai nguyên nhân CodeX chỉ ra, mình kiểm code xác nhận ĐÚNG:
1. **`registry_so_far` bơm vào MỌI window** (`runner.py:142` merge → `:288/:336` gửi lại
   registry đang phình) → input bùng nổ ở chương sau (linear: 51 call/400k prompt token).
   **Fix:** D2L window KHÔNG bơm full registry (nén tối thiểu/bỏ); consolidation lo nhất quán.
   Reviewer add: bỏ registry-injection còn ĐÚNG phương pháp hơn (window độc lập, tái lập được).
   Sau fix PHẢI đo lại recall dev (giữ ≥0.5).
2. **Quota guard dùng `date.today()` local** (`llm_client.py:140,304`) trong khi OpenAI tính
   UTC → guard under-count, để vượt 2.5M UTC. **Fix:** guard dùng UTC (LOCK ee validity).
Thêm: preflight estimator (window còn lại × token TB). **Update sau CodeX §5.0:** rerun bằng
`OPENAI-KEY-2` đã hoàn tất đủ 4 chương, DB đã freeze, final C recall 0.742331 / agreement 0.743802,
`agent→tác nhân` ĐÃ BẮT. Claude cần thay verdict cũ bằng review mới.

### 6.7 REWORK VERDICT: PASS (Claude, 2026-06-14) — token-fix + recall đều đạt
Rework đạt MỌI mục tiêu; số tái tính độc lập từ DB/report, KHỚP.

**Tái xác minh:**
- recall **0.123 → 0.742** (121/163 gold matched); `agent → tác nhân` captured VÀ khớp gold; frozen=1; gold 458 nguyên.
- **Token-bloat ĐÃ HẾT (bằng chứng cache key2):** prompt/call 974–2146, avg **1365** (trước: 1.2k→10.8k). Benchmark 762k token (trước ~2M). Fix bỏ-registry ăn rõ.
- Guards có đủ + có test: UTC date (`_usage_date_utc`), per-call ceiling (`PromptTokenCeilingExceeded`, cap 6000), preflight (`estimate_prompt_tokens` trước call), `compress()` cắt glossary. 107 tests pass.
- Directional Lock GIỮ: build path không query gold (chỉ câu cấm trong prompt). Key `OPENAI-KEY-*.txt` gitignore, không log. Baseline lỗi cũ giữ ở `data/_baseline/` để audit.

**31 conflicts = TOÀN BỘ là biến thể hợp lệ, KHÔNG phải lỗi** (đã soi cả 31): khác thứ tự
(`exploding gradient`↔`bùng nổ gradient`), chính tả (`tỷ`/`tỉ`, `cosin`/`cô-sin`), đồng nghĩa
(`đối tượng`/`vật thể`, `họ`/`nhóm`), dịch-hay-giữ-Anh (`batch`→`lô` vs `batch`). Nhiều chỗ
Builder CHÍNH XÁC HƠN gold (`attention`→`chú ý` not `tập trung`; `implicit`→`ngầm`); gold còn
lỗi chính tả (`biễu diễn`). → **Bằng chứng thật cho doctrine (dd): agreement exact-match
0.744 ĐÁNH GIÁ THẤP chất lượng Builder; với allowed_variants curate gold thì phần lớn 31 này
thành agreement.** Builder học thuật ngữ hợp ngôn ngữ, đôi khi hơn cả bản người.

**3 follow-up cho P3-D2L (KHÔNG chặn PASS):**
1. **Lọc occ≥2 khi INJECTION (sắc hơn 'termhood', theo phát hiện user 2026-06-14):** registry có
   **743/1608 (46%) term occ≤1 (hapax)** — hapax không thể trôi → đóng góp consistency ≈0 (gồm rác
   `.shape`/`16kHz`/`1:1 correspondence`). Lọc occ≤1 khỏi injection: registry 1608→865 (-46%), giữ
   96/123 gold matched (27 mất đều hapax = mất ~0 consistency). Builder VẪN trích hết + đếm occurrence
   (cột `occurrences_count` đã có); CHỈ bước inject lọc occ≥2 (tách 'biết gì' khỏi 'ép gì'). Report
   recall TÁCH: flat (vs all gold, = Builder quality) + recurring (occ≥2, = consistency-relevant).
   Caveat: tính occurrence ở quy mô đang dịch (sách dài tần suất khác). Fix nhỏ: 71 term occ=0 dù có
   evidence = counting gap, occurrence phải ≥1 nếu có evidence.
2. **Chuẩn hóa source-term:** `ground-truth` vs `ground truth` → 2 VI khác (`chân trị`/`nhãn thật`),
   không consolidate. Cần chuẩn hóa hyphen/space/case trong khóa gộp.
3. **allowed_variants nhiễu:** variants của `agent` gồm `đặc vụ`/`đại diện` (sai nghĩa). → P3-D2L
   chỉ bơm **canonical** cho Translator (không bơm variants), và ruler của B = gold-curated
   variants (dd), KHÔNG dùng variants Builder tự đề.

→ **P2-D2L PASS.** registry (recall 0.742, agent captured, token kỷ luật) đủ làm nền cho P3-D2L.
