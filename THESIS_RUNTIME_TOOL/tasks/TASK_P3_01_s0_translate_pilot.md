# TASK_P3_01_s0_translate_pilot — S0 end-to-end: dịch 2 chương bằng WINDOW + số S0-vs-oracle đầu tiên

- **Status:** READY
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

—

## 6. Review *(Claude điền)*

—
