# TASK_P2_D2L_world_builder — World Builder D2L (chế độ thuật ngữ kỹ thuật) + chẩn đoán C (Builder-vs-gold) + FREEZE

- **Status:** REWORK/BLOCKED (CodeX rework in progress; full benchmark blocked by API quota on 2026-06-14; see §5)
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

### 5.1 Files changed / added

- Added DB-backed prepass loader: `pipeline/prepass/db_source.py`.
  - Resolves spec slugs like `introduction`, `linear_networks` to real DB chapter ids
    `d2l_introduction`, `d2l_linear_networks`.
  - D2L World Builder reads heading + translate/prose blocks from `blocks.original_text`;
    code/math/image passthrough blocks stay out of the prompt to avoid token bloat.
- Added prompt mode `d2l_terminology` in `pipeline/prepass/prompt.py`.
  - Current locked prompt version after REWORK: `d2l_terminology_v6`.
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

### 5.3 Commands run so far

```text
python -m pytest pipeline/tests/test_d2l_builder.py -v
8 passed in 4.74s

python -m pytest pipeline/tests/ -v
105 passed in 62.10s

python -m pipeline.scripts.run_prepass --db data/jobs/d2l_p1/memory.sqlite3 --chapters deep_learning_computation --mode d2l_terminology
prompt_version: d2l_terminology_v6
chapter: d2l_deep_learning_computation
status: passed
calls: 28
terms: 396
windows: 28
json_fail_rate: 0.0
reasoning_tokens: 0
incremental_cost_usd: 0.1144585

DEV C-gate on a copied DB:
gold_terms_present: 40
builder_terms: 252
matched_terms: 23
agreement_terms: 19
recall: 0.575
agreement: 0.826087
missing_terms: 17
conflicts: 4
extra_terms: 229
```

Pytest on this Windows machine prints an ignored cleanup warning after pass:
`PermissionError: [WinError 5] Access is denied: 'D:\\temp\\pytest-of-Snail\\pytest-current'`.
It occurs after pytest reports success and did not change test status.

### 5.4 Benchmark attempt status

Full benchmark command attempted:

```text
python -m pipeline.scripts.run_prepass --db data/jobs/d2l_p1/memory.sqlite3 --chapters introduction preliminaries linear_networks multilayer_perceptrons --mode d2l_terminology --freeze
```

Result on 2026-06-14:

- `d2l_introduction` passed: 48 windows, 654 raw window terms.
- `d2l_preliminaries` passed: 50 windows, 587 raw window terms.
- `d2l_linear_networks` passed: 51 windows, 807 raw window terms.
- `d2l_multilayer_perceptrons` started and cached 2/78 windows, then the provider returned
  `429 insufficient_quota`.
- No final `run_report.json` was written.
- Main DB remained clean and unfrozen:
  - `glossary_entries=0`
  - `memory_items=0`
  - `eval_glossary_gold=458`
  - `memory_frozen=0`

The overrun was the daily TOKEN quota (2.5M), not money: OpenAI CSV showed 2.735M tokens on
2026-06-13 UTC for the thesis project, costing only ~$0.2536. The `$4.96/$5.00` screenshot was an
UNRELATED project (gpt-5.5), NOT this thesis — do not read it as the thesis burning $5.

### 5.5 Partial diagnostic evidence, not acceptance

To sanity-check the rework without touching the main DB, CodeX built memory from the three completed
benchmark artifacts into a copied DB only. This is **not** the acceptance result because the fourth
benchmark chapter is missing.

```text
3-chapter partial C on copied DB:
gold_terms_present: 137
builder_terms: 1061
matched_terms: 96
agreement_terms: 66
recall: 0.70073
agreement: 0.6875
missing_terms: 41
conflicts: 30
extra_terms: 965
```

Important sanity checks from the copied DB:

- `agent -> tác nhân` is now captured.
- `model -> mô hình` is captured.
- `linear regression -> hồi quy tuyến tính` is captured.
- `softmax regression -> hồi quy softmax` is captured.
- `loss function -> hàm mất mát` is captured.

Interpretation: REWORK fixed the original C-gate failure mode (`agent` missing and recall 0.123), but the task
cannot move to REVIEW until quota is restored and the full 4-chapter benchmark can finish, persist, freeze,
and produce `data/reports/d2l_builder_vs_gold.json`.

## 6. Review *(Claude điền)*

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
Thêm: preflight estimator (window còn lại × token TB). Rerun sau quota reset — 3 chương đầu
đã cache, chỉ còn ~76 window MLP. **Tín hiệu tốt: recall 0.123→0.575 dev / 0.70 partial,
`agent→tác nhân` ĐÃ BẮT, main DB sạch/unfrozen.** CHƯA PASS tới khi full 4 chương + freeze + C cuối.
