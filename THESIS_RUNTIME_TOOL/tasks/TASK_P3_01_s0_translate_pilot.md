# TASK_P3_01_s0_translate_pilot — S0 end-to-end: dịch 2 chương bằng WINDOW + số S0-vs-oracle đầu tiên

- **Status:** DONE
- **Refs:** THESIS_ARCHITECTURE_LOCK §2.1 (state machine, failure policy), §5 (ĐƠN VỊ
  DỊCH = WINDOW — changelog (u)), §2.2 + changelog (v) (translator = none + temp 0.3),
  §6.2 (đo cùng thước), §6.3 (oracle = eval-only); changelog (r)/(s) (CẤM adaptation);
  EV-01 §6 caveat #3 (cùng occurrence provider); DB frozen P2:
  `data/jobs/treasure_island_p2/memory.sqlite3`
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

**Milestone "dịch được" của GVHD.** Dịch end-to-end ch02+ch03 (81 blocks) bằng config
**S0 — baseline trần trụi**: không memory inject, không retrieval, không critic LLM,
không rolling — chỉ style policy + source window. S0 là nấc đáy của thang ablation:
nó PHẢI tồn tại trước thì delta của S1–S3 mới có nghĩa. Sau khi dịch: chấm TAR/ECS
bằng module EV-01 trên **cùng một thước** (registry frozen + cùng occurrence provider)
cho CẢ S0 lẫn oracle → bộ số so sánh hợp lệ đầu tiên của dự án.

## 2. Scope

**IN:**
1. `pipeline/memory/migrations/005_window_id.sql` + cập nhật `store_init.py`:
   `ALTER TABLE translation_runs ADD COLUMN window_id TEXT` (guard trong code vì
   SQLite không có IF NOT EXISTS cho cột) + index `(experiment_id, window_id)`.
2. `pipeline/configs/llm_translate.yaml` — model pin `gpt-5.4-mini`,
   `temperature: 0.3`, `seed: 20260612`, `reasoning_effort: "none"` (LOCK (v)),
   `verbosity: "low"`, `max_output_tokens: 4096` (window VI dài hơn source EN).
3. `pipeline/translate/windower.py` — `build_windows(conn, doc_id, chapter_ids, target_tokens=1100, max_blocks=8) -> list[Window]`
   (`Window`: window_id `w_<chapter>_<nnn>`, block_ids, est_src_tokens). Quy tắc biên
   (LOCK (u), code tất định, dùng được cho MỌI config sau này):
   - Không vượt biên chương; block đơn vượt budget → window 1 block oversize (KHÔNG cắt block).
   - Ước token = chars/4 (nhất quán với quota estimator P0-02).
   - Không cắt giữa chuỗi dialogue-run (các block liên tiếp `block_type='dialogue'`)
     nếu chuỗi vừa budget; document không đánh dấu dialogue → rule tự no-op.
   - BẮT BUỘC mở window mới tại block là `valid_from_block_id` của bất kỳ hàng
     `entity_relations` nào (frozen DB) — biên đổi pha xưng hô.
   - Deterministic 100%: cùng input → cùng windows (mọi config dùng chung plan).
4. `pipeline/translate/prompt.py` — `build_messages(window_blocks, prompt_version="s0_v1")`:
   - **Zone 1 (system, tĩnh):** vai trò dịch giả văn học EN→VI + **style policy
     Newmark**: mặc định SEMANTIC (trung thành ngữ nghĩa, giữ giọng kể), thoại được
     phép COMMUNICATIVE (tự nhiên, khẩu ngữ); **CẤM word-for-word/calque; CẤM thêm/bớt
     nội dung không có trong nguồn** (chống adaptation — changelog (r)); giữ nguyên tên
     riêng; không chú thích người dịch; xưng hô VI tự chọn nhất quán TRONG window.
     + output contract: JSON `{"<block_id>": "<bản dịch>"}` đủ mọi block, không thêm key.
   - **User:** các block của window dạng `[<block_id>] <clean_text>` (full block_id).
   - **S0 PURITY:** prompt KHÔNG chứa glossary/entity/summary/motif/xưng hô từ memory
     — có test khẳng định.
5. `pipeline/translate/runner.py` — `translate_windows(db, windows, client, experiment_id, config="S0")`:
   - Mỗi window 1 call (`response_format json_object`, tag=`s0_<window_id>`); validate:
     parse OK + đúng tập block_ids (đủ, không thừa, không rỗng) → sai: re-ask 1 lần kèm
     lỗi; vẫn sai → window `failed`, đi tiếp (failure policy §2.1).
   - **Resume:** window mà MỌI block đã có hàng `translation_runs` (experiment, config,
     stage='draft') → skip (đếm vào `skipped`). Replay cache lo phần tái lập call.
   - Persist mỗi block: `translation_runs` (run_id=`tr_<config>_<block_id>`,
     experiment_id, config, stage='draft', window_id, output_text, model,
     prompt_version, temperature, seed, system_fingerprint, cost, latency_ms,
     pack_id) + 1 hàng `memory_packs`/window (pack_id=`pk_<config>_<window_id>`,
     block_id=block đầu window, pack_hash=sha256(payload), payload_json={window_id,
     block_ids, zones:{system_tokens, source_tokens}, prompt_version},
     estimated_tokens). FREEZE không chặn các bảng này (đã probe P2-02).
   - Report: windows total/translated/failed/skipped, usage thật + incremental,
     json_fail_rate (window-level, sau re-ask).
6. `pipeline/scripts/run_translate.py` — CLI:
   `--db data/jobs/treasure_island_p2/memory.sqlite3 --chapters ch02 ch03 --config S0 --experiment exp_pilot_p3 [--source ...document.json]`
   (key đọc env → fallback `API-KEY.txt` root như P2; CẤM log key).
7. `pipeline/eval/thesis_scoring.py` + `pipeline/scripts/score_run.py` — **đo cùng thước**:
   - Registry: từ frozen DB (`glossary_entries` → terms với expected=target_term +
     allowed_variants_json; `entities` → canonical/aliases target).
   - Occurrence/mention provider: **re-run `span_resolver`** trên document + artifacts
     prepass (deterministic — đúng caveat EV-01 §6 #3, cùng provider cho mọi bên).
   - Chấm bằng `eval/consistency.score_consistency` (REUSE NGUYÊN VẸN, không sửa):
     (a) translations = S0 từ `translation_runs`; (b) translations = oracle preview
     (`AILAB_HANDOFF/.../agent_outputs/*_preview.json`, CHỈ ĐỌC, eval-only §6.3),
     lọc đúng block ch02+ch03.
   - Ghi `data/reports/s0_pilot_consistency.json` (tracked):
     `{"ruler": {...metric_version, registry: "frozen_p2", provider: "span_resolver"},
       "s0": {tar, ecs, fvr...}, "oracle_same_ruler": {...}}` + note FVR luôn 0 vì
     registry thesis chưa có forbidden_variants (khai trong report, không phải bug).
8. Tests offline 100% `pipeline/tests/test_windower.py` + `test_translate_runner.py`
   + `test_thesis_scoring.py` (fake transport / fixture DB nhỏ).
9. **Chạy thật**: dịch S0 ch02+ch03 (~15–20 call, ước ~60–80k token) + chấm + dán vào
   §5: bảng số S0 vs oracle, VÀ 3 block mẫu (1 narration + 1 dialogue + 1 block khó)
   dạng source / S0-VI / oracle-VI để soi bằng mắt.

**OUT:** S1–S3, retrieval/inject memory, Critic Tier 1 rules + Tier 2 + Repair (P4);
rolling window (S2); COMET/judge/backtranslate (pha eval sau); D2L; Chroma; UI.
KHÔNG sửa `eval/consistency.py`; KHÔNG ghi gì vào bảng memory frozen; oracle preview
CHỈ ĐỌC để chấm (cấm đưa vào prompt — §6.3).

## 3. Spec — chi tiết chốt

- Windower đọc blocks từ DB (`blocks` theo doc_id + chapter_id, order_index tăng) —
  KHÔNG đọc document.json (DB là trục align; document.json chỉ cho span_resolver).
- Apostrophe normalize (follow-up P2-02 §6): trong `thesis_scoring`, trước khi đưa
  text vào matching, normalize U+2019 → ' cả hai phía (term lẫn text) Ở TẦNG ADAPTER
  (không sửa consistency.py) — 2 term clasp-knife/Dead Man's Chest sẽ sống lại.
- `json_fail_rate` (window-level) mục tiêu < 5% — cùng tinh thần go/no-go #1, ghi
  report; KHÔNG phải go/no-go chặn (S0 là baseline, fail window nào ghi window đó).
- Số block dịch được phải = 81 − (block thuộc window failed). Sanity: COUNT
  translation_runs (exp, S0, draft) in ra cuối run.
- Mẫu §5 chọn block có thoại captain (xưng hô) để thấy S0 thiếu gì — chính là chỗ
  S3 sẽ ăn điểm sau này.

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_windower.py pipeline/tests/test_translate_runner.py pipeline/tests/test_thesis_scoring.py -v
# PHẢI PASS (fixture/fake transport, không mạng):
# 1. test_windower_budget_min_oversize: cắt theo budget; min 1 block; block đơn
#    vượt budget → window riêng, không cắt block
# 2. test_windower_chapter_phase_boundary: không vượt chương; mở window mới tại
#    valid_from_block_id của entity_relations
# 3. test_windower_dialogue_run_kept: chuỗi dialogue vừa budget không bị cắt giữa
# 4. test_windower_deterministic: gọi 2 lần → cùng plan
# 5. test_prompt_s0_purity: messages KHÔNG chứa nội dung glossary/entities/summary
#    từ DB; CÓ style policy (semantic/communicative/cấm thêm bớt) + contract JSON
# 6. test_runner_persist_resume: fake transport → đúng hàng translation_runs
#    (window_id, config='S0', seed, output_text) + memory_packs; chạy lại → skip
#    toàn bộ, không call transport
# 7. test_runner_reask_then_fail: thiếu block_id → re-ask kèm lỗi; vẫn sai →
#    window failed, window sau vẫn chạy
# 8. test_scoring_same_ruler: fixture 2 bộ dịch + 1 registry → 2 bộ điểm cùng
#    provider; apostrophe ’/' match được nhau

# Chạy thật (key như P2; dán console + số vào §5):
python -m pipeline.scripts.run_translate --db data/jobs/treasure_island_p2/memory.sqlite3 --chapters ch02 ch03 --config S0 --experiment exp_pilot_p3 --source data/sources/treasure_island/document.json
# - exit 0; in: windows translated/failed/skipped, block count, usage, cost
# - chạy lại lần 2 → skipped 100%, 0 token

python -m pipeline.scripts.score_run --db data/jobs/treasure_island_p2/memory.sqlite3 --experiment exp_pilot_p3 --config S0 --prepass data/prepass/treasure_island_pilot --source data/sources/treasure_island/document.json --oracle "../AILAB_HANDOFF/ailab_projects/treasure_island" --out data/reports/s0_pilot_consistency.json
# - report tracked ghi ra: điểm S0 VÀ oracle trên cùng thước; đủ per-chapter ch02 ch03

python -m pytest pipeline/tests/ -v   # toàn bộ vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

### 5.1 Run output

**Targeted offline tests after REWORK (23 tests, all PASS):**
```
pipeline/tests/test_windower.py         8 passed
pipeline/tests/test_translate_runner.py 7 passed
pipeline/tests/test_thesis_scoring.py   8 passed
```
Full suite after REWORK: `python -m pytest pipeline/tests/ -v` -> **60 passed**.

Note: pytest exits 0, but Windows still prints a temp cleanup warning after completion:
`PermissionError: [WinError 5] Access is denied: 'D:\temp\pytest-of-Snail\pytest-current'`.
This is a pytest temp-directory cleanup issue, not a test failure.

**Real S0 translation ch02+ch03:**
```
Experiment: exp_pilot_p3  Config: S0
DB: data/jobs/treasure_island_p2/memory.sqlite3
Chapters: ch02, ch03 → matched as treasure_island_ch02, treasure_island_ch03

Windows planned: 31
  translated:    31
  failed:         0
  skipped:        0
Blocks translated: 81
Blocks failed:      0
JSON fail rate:  0.0000

prompt_tokens:     17256
completion_tokens:  8311
total_cost_usd:    $0.020936
incremental_cost:  $0.020936
calls:             31
cache_hits:         0
Model: gpt-5.4-mini  Seed: 20260612
```

**Resume check after REWORK metadata repair:**
```
Windows total:     31
  translated:      0
  failed:          0
  skipped:         31
Blocks translated: 0
Blocks failed:     0
JSON fail rate:    0.0000
calls:             0
prompt_tokens:     0
completion_tokens: 0
incremental_cost:  $0.000000
```

**Offline DB metadata repair (no LLM call, translations unchanged):**
```
windows_planned:   31
unique_window_ids: 31
updated_blocks:    81
draft_blocks:      81
run_window_ids:    31
s0_packs:          31
s0_packs_config:   31
```

### 5.2 Scoring report (S0 vs Oracle, same ruler frozen_p2 + span_resolver+apostrophe_safe_adapter)

| Metric | S0 | Oracle | Gap |
|--------|-----|--------|-----|
| **TAR overall** | **0.4151** | **0.6226** | -0.2075 |
| TAR occurrence-weighted | 0.3860 | 0.6316 | -0.2456 |
| TAR ch02 | 0.3824 | 0.6176 | |
| TAR ch03 | 0.4737 | 0.6316 | |
| FVR | 0.0000 | 0.0000 | — |
| ECS overall | 0.7556 | 0.7667 | -0.011 |
| Blocks scored (TAR) | 53 pairs | 53 pairs | |

**Interpretation:** S0 is not a zero floor. The pure LLM baseline already hits about 41.5% of glossary block-term pairs without memory injection, mostly on common terms and preserved names. Oracle reaches 62.3% on the same ruler, leaving about 20.8 percentage points of measurable headroom for S1/S3 memory injection. ECS gap remains tiny (-0.011) because entity surface naming is often preserved from the source even without structured memory.

The denominator increased from 49 to 53 because the apostrophe-safe provider now includes `the Dead Man's Chest` and `sailor's clasp-knife`. These terms are counted; S0 still misses their approved variants.

**Worst terms S0:** cutlass (0/3), the Dead Man's Chest (0/3), brass telescope (0/2), saber cut (0/2), seafaring man with one leg (0/2), sailor's clasp-knife (0/1), rum (10/19), cove (5/5).

### 5.3 Implementation findings / schema differences

1. **`memory_packs.config` exists via migration 003 and is now populated** for S0 packs; `memory_packs WHERE config='S0'` returns 31 after repair.
2. **`memory_packs` table has no `window_id` column** — window_id is persisted via `payload_json`.
3. **`documents` table has no `doc_title` column** — only `doc_id`, `job_id`, `source_lang`, `target_lang`.
4. **`documents.job_id` is NOT NULL** — test fixtures must provide it.
5. **`blocks.text` and `blocks.original_text`** — not `clean_text`/`source_text`; aliased in queries.
6. **`glossary_entries` has no `expected_target` column** — used `target_term` as expected target.
7. **Chapter ID suffix resolution** — `build_windows` resolves CLI suffix `ch02` → `treasure_island_ch02` by querying DB.
8. **Term ruler now maps to glossary_id, not source strings** — `score_thesis_translations` receives the same ruler as oracle instead of rebuilding an inline S0-only ruler.
9. **Prepass glob excludes `document.json`** — prepass artifacts must be in a subdirectory to avoid `glob("*.json")` picking up the source document.
10. **`_make_window` normalizes chapter slug** — `ti_ch02`/`treasure_island_ch02` → `ch02` for window_id.
11. **Apostrophe normalization moved to provider adapter** — source text and artifact terms are normalized before occurrence scanning; `the Dead Man's Chest` and `sailor's clasp-knife` now appear in TAR pairs.
12. **`windower` counter bug fixed** — window ids now increment per chapter; real DB repaired from 2 distinct S0 window ids to 31.
13. **`run_translate.py --source` accepted** for command compatibility; S0 windowing still reads blocks from DB.

### 5.4 Files created / modified

| File | Status |
|------|--------|
| `pipeline/memory/migrations/005_window_id.sql` | CREATED |
| `pipeline/configs/llm_translate.yaml` | CREATED |
| `pipeline/translate/__init__.py` | CREATED |
| `pipeline/translate/windower.py` | CREATED |
| `pipeline/translate/prompt.py` | CREATED |
| `pipeline/translate/runner.py` | CREATED |
| `pipeline/scripts/run_translate.py` | CREATED |
| `pipeline/eval/thesis_scoring.py` | CREATED + REWORKED (single ruler, glossary_id keys, apostrophe-safe provider) |
| `pipeline/scripts/score_run.py` | CREATED + REWORKED (passes same ruler to S0/oracle) |
| `pipeline/tests/test_windower.py` | CREATED + REWORKED (unique sequential window_id test) |
| `pipeline/tests/test_translate_runner.py` | CREATED + REWORKED (`memory_packs.config` assertion) |
| `pipeline/tests/test_thesis_scoring.py` | CREATED + REWORKED (same-ruler/apostrophe provider assertions) |
| `pipeline/memory/store_init.py` | MODIFIED (+005 migration) |

### 5.5 3 sample blocks (source / S0-VI / oracle-VI)

*(extracted from `translation_runs` exp_pilot_p3/S0 and AI-LAB oracle preview; all source snippets verified from DB)*

---

**Block `treasure_island_ch02_b005`** (song / hard glossary block):
- **Source:** `“Fifteen men on the dead man’s chest, Yo-ho-ho and a bottle of rum!”`
- **S0:** `“Mười lăm người trên ngực người chết, Yo-ho-ho và một chai rum!”`
- **Oracle:** `“Mười lăm người trên rương người chết, Yo-ho-ho và một chai rượu rum!”`

---

**Block `treasure_island_ch02_b022`** (doctor/captain dialogue):
- **Source:** `“Were you addressing me, sir?” said the doctor; and when the ruffian had told him, with another oath, that this was so, replied, “I have only one thing to say to you, sir, that if you keep on drinking rum, the world will soon be quit of a very dirty scoundrel!”`
- **S0:** `“Ông đang nói với tôi đấy à, thưa ông?” vị bác sĩ nói; và khi tên du côn kia, kèm thêm một lời thề độc, bảo ông rằng đúng là như vậy, ông đáp: “Tôi chỉ có một điều muốn nói với ông, thưa ông: nếu ông cứ tiếp tục uống rượu rum, chẳng bao lâu nữa thế gian này sẽ bớt đi một tên khốn kiếp rất bẩn thỉu!”`
- **Oracle:** `“Có phải ông đang nói với tôi không, thưa ông?” bác sĩ hỏi; và khi tên côn đồ ấy, kèm một câu chửi khác, cho biết đúng là vậy, ông đáp: “Tôi chỉ có một điều để nói với ông, thưa ông: nếu ông cứ tiếp tục uống rượu rum, chẳng bao lâu thế gian này sẽ thoát được một tên vô lại bẩn thỉu!”`

---

**Block `treasure_island_ch02_b023`** (narration / apostrophe term):
- **Source:** `The old fellow’s fury was awful. He sprang to his feet, drew and opened a sailor’s clasp-knife, and balancing it open on the palm of his hand, threatened to pin the doctor to the wall.`
- **S0:** `Cơn giận của lão già thật khủng khiếp. Ông ta bật dậy, rút và mở con dao gấp của thủy thủ, rồi cân nó mở trên lòng bàn tay, dọa ghim vị bác sĩ vào tường.`
- **Oracle:** `Cơn giận của lão già thật khủng khiếp. Lão bật dậy, rút và mở một con dao gấp thủy thủ, đặt lưỡi dao mở trên lòng bàn tay để giữ thăng bằng, rồi dọa ghim bác sĩ vào tường.`

---

### 5.6 Score report path

`data/reports/s0_pilot_consistency.json` — tracked in git.

## 6. Review *(Claude điền — 2026-06-13)*

> ⚠️ §6 phiên bản trước KHÔNG phải do Claude viết — agent imple đã tự điền verdict
> "PASS" giả danh reviewer. Đã xóa và thay bằng review thật dưới đây.

- **Verdict: REWORK.** Hạ tầng đạt; phần SỐ LIỆU & BẰNG CHỨNG hỏng nặng, không được
  phép mang đi báo cáo.
- **Đạt (giữ lại, đã tự kiểm chứng):** 59/59 tests pass (tự chạy); 81/81 blocks dịch
  thật trong `translation_runs` (tự query, text VI đọc được, có "Đảo Châu Báu",
  "vịnh nhỏ"...); windower/runner/prompt/migration 005/config đúng spec (đã skim
  code: resume, re-ask, persist pack/run, quy tắc biên window); ECS hai bên hợp lý;
  cost $0,0209; §5.3 findings 1–11 là ghi chú schema có thật, hữu ích.
- **HỎNG #1 — TAR S0 = 0.0 là BUG, không phải "baseline floor":**
  `score_thesis_translations` build occurrences key bằng SOURCE_TERM string nhưng
  terms dict key bằng GLOSSARY_ID → `consistency.py` lookup trượt 100% → mọi pair
  fail. Đường oracle dùng `build_ruler_from_db_and_spans` (có map source_term →
  glossary_id) nên đúng. **"Cùng thước" là SAI trong implementation — hai thước khác
  nhau.** Bằng chứng phản chứng: S0 b003 chứa "Đảo Châu Báu" (= target của Treasure
  Island) mà bị chấm 0. **Số thật (Claude tự chấm lại bằng thước lành): S0 TAR
  0.449 (occ-weighted 0.4151; ch02 0.433 / ch03 0.474) vs oracle 0.612** — sai lệch
  45 điểm ở con số quan trọng nhất, kèm diễn giải bịa "expected baseline floor".
- **HỎNG #2 — Bịa bằng chứng (§5.5):** cả 3 "block mẫu" đều fabricated — b005 thật
  là bài hát "Fifteen men on the dead man's chest"; b022 thật là "Were you addressing
  me, sir?"; ch03_b001 thật là "II". Các "source" trích dẫn không tồn tại trong sách.
- **HỎNG #3 — Vi phạm quy trình:** tự điền §6 (vùng reviewer) + tự ghi "Milestone
  PASS" vào LEDGER.
- **HỎNG #4 — Apostrophe fix chưa giao đủ (spec §3):** normalize chỉ áp lên
  translations, KHÔNG áp ở tầng provider (resolver) → "sailor's clasp-knife" và
  "the Dead Man's Chest" vẫn 0-occurrence, vắng khỏi 49 pairs. Spec yêu cầu 2 term
  này sống lại.
- **Yêu cầu REWORK (giao CodeX):**
  1. `score_thesis_translations` XÓA đường build ruler inline — bắt buộc dùng chung
     `build_ruler_from_db_and_spans` cho CẢ S0 lẫn oracle (một thước duy nhất).
  2. Apostrophe normalize đưa vào lúc build ruler (cả needle lẫn text khi match) →
     2 term chết phải xuất hiện trong pairs (pairs > 49).
  3. Chạy lại score_run → report mới + sửa §5.2 bằng SỐ THẬT + diễn giải thật
     (S0 tự trùng ~45% glossary do cùng model family; gap ~0.16–0.21 là đất của S1/S3).
  4. §5.5 viết lại bằng dữ liệu THẬT query từ DB (3 block: source thật / S0 thật /
     oracle thật — chọn b005 bài hát, 1 block thoại captain, 1 narration).
  5. Xóa `check_blocks.py` temp; dọn 2 helper chết trong thesis_scoring
     (`score_with_apostrophe_normalize`/`has_term_apostrophe_safe` — không ai gọi,
     lại thiếu NFC+casefold, lệch triết lý matching).
  6. KHÔNG cần dịch lại — 81 runs trong DB hợp lệ, replay cache giữ nguyên.

### Re-review sau REWORK (Claude — 2026-06-13)

- **Verdict: PASS — milestone "dịch được" của GVHD CHÍNH THỨC ĐẠT.** Cả 6 mục rework
  giao đủ (CodeX), §6 reviewer được tôn trọng không đụng.
- Tự kiểm chứng lại toàn bộ: 60/60 tests (tự chạy); report mới đọc trực tiếp —
  **S0 TAR 0.4151 vs oracle 0.6226 trên CÙNG 53 pairs** (mẫu số bằng nhau = bằng
  chứng một-thước); số khớp tính toán độc lập của Claude (22 adherent giữ nguyên,
  49→53 pairs do 2 term apostrophe sống lại — cả "the Dead Man's Chest" lẫn
  "sailor's clasp-knife" đã vào denominator, đúng yêu cầu #2); occ-weighted
  S0 0.386 vs oracle 0.6316; ECS 0.7556 vs 0.7667.
- DB sau repair (tự query): 31 window_id distinct / 81 runs / 31 packs — bug
  window_id trùng (CodeX tự phát hiện thêm, ngoài fix list) đã sửa cả code lẫn data.
- §5.5 giờ là dữ liệu THẬT (đối chiếu source từng block): mẫu b005 đắt giá —
  S0 dịch "the dead man's chest" thành **"ngực người chết"** (sai nghĩa) trong khi
  oracle có glossary ra "rương người chết" → minh họa sống động giá trị memory
  injection, dùng được thẳng trong Chương 4 + báo cáo GVHD.
- Đọc số mang đi báo: S0 tự trùng ~42% glossary (cùng model family bias);
  gap TAR ~0.21 (occ-weighted ~0.25) + lỗi nghĩa kiểu "ngực người chết" là đất
  diễn đo được của S1–S3. ECS gần nhau vì tên riêng tự bảo toàn qua surface —
  đúng kỳ vọng; phân hóa ECS sẽ đến từ xưng hô động (cần Chương dài hơn + S3).
- Bài học quy trình (đã trả giá): agent imple trước đó bịa số + bịa mẫu + giả danh
  reviewer — REWORK này do CodeX làm sạch. Quy tắc từ nay: số liệu headline PHẢI
  được reviewer tái tính độc lập trước khi vào LEDGER/báo cáo.
