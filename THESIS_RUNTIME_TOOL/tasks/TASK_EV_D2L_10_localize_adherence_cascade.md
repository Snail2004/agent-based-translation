# TASK EV-D2L-10 — Occurrence localization+adherence CASCADE (4 tầng: region → code-rules → LLM-GPT → human); CHẠY-để-đo, không ước lượng

Status: REVIEW (CodeX implemented §5, STOP, không commit) → Claude review §6 + commit.
Type: EVAL CASCADE + method-decision. eval-only, frozen DB `mode=ro`. KHÔNG đổi headline metric tới khi §6 adopt. T3 = GPT (cloud, OPENAI key).

- **Refs:** EV-D2L-08 (count adherence + JOINT cross-term allocation, `occurrence_adherence.py`) · EV-D2L-09 (`region_align.py` bge-m3 top-1 + margin→top-2; `abstain`/`reject`) · EV-D2L-07b/07b-3 (`localizer_cascade.py` máy LLM + code-anchor locator position-primary `code_anchor_v3_position`) · memory: `prompt-memory-design-is-first-class`, `token-growth-halt-and-audit`, `dont-tune-intervention-on-test-baseline`, `scoring-scope-equals-production-scope`, `ev09-sentence-region-narrowing-findings`, `occurrence-weighted-block-anymatch-inflation`.
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

Kiến trúc 4 tầng đã KHÓA (sơ đồ trong hội thoại): **T1 khoanh câu (bge-m3) → T2 luật code (0 LLM) → T3 LLM-GPT phân xử phần dư → T4 human chốt cuối + sinh gold**. Việc này **chạy cascade trên corpus thật để ĐO**, từ chối ước lượng mù 4 con số: (i) %T2 giải xong, (ii) %dư phải gọi LLM, (iii) %chạm human, (iv) **tỉ lệ đội-lốt thật**.

**Vì sao "chạy mới biết" khả thi mà không phase nào phải đoán phase sau:** T1+T2 chạy (0 LLM, 0$) làm **residual LỘ RA** (đo, không đoán) → mới gắn T3-GPT chạy **đúng residual** → human gán **đúng cái T3 đẩy lên** (T4 vừa là chốt cuối vừa là gold validate T3). Cost LLM và công human đều biết-trước-khi-tiêu.

**Đội-lốt (masquerade) — vì sao 2 bộ gold cũ không đo được:** EV-08 credit theo ĐẾM `min(source_count, active_target_count)`; một occurrence bị BỎ vẫn có thể bị credit nhầm nếu một accepted-form "lạc" (của từ khác) bù vào đếm. Đội-lốt sống trong ca `not_rendered` — mà 102 collision gold (100% `rendered`) và 118 localizer gold đều KHÔNG có ca `not_rendered` (đối chiếu: chỉ 23/428 occurrence at-risk trùng, 0 ca not_rendered). Cascade làm đội-lốt LỘ RA như một **nghi-can có cấu trúc**: *occurrence được T2 credit nhưng accepted-form KHÔNG nằm trong câu 1:1 của chính nó (theo T1)* → cái credit đó đến từ câu khác.

Discipline (không vi phạm):
- Scope chạy = **registry ruler** (đúng glossary hệ thống inject) = production scope (`scoring-scope-equals-production-scope`). KHÔNG dùng gold ruler (eval-only).
- T1 = bge-m3 (KHÓA EV-09). KHÔNG position trong production (chỉ hòa trên D2L giữ thứ tự, gãy literary). KHÔNG model embedding mới.
- **Prompt T3 = lõi luận văn:** Claude soạn → **USER duyệt TRƯỚC khi gọi GPT 1 lần nào** (`prompt-memory-design-is-first-class`). Lock a priori; KHÔNG tinh chỉnh prompt trên gold test (`dont-tune-intervention-on-test-baseline`).
- DEV chọn thiết kế; **KHÔNG phải trust number** (held-out + literary = task riêng).
- Confidence T3 KHÔNG được tin mù; cổng phải **kiểm định trên gold** (low-conf ↔ thật sự sai). Tái dùng từ vựng EV-09: `not_found`≈reject, `ambiguous`≈abstain.

## 2. Scope

**IN:**
1. **Cascade driver** chạy MỌI source-term occurrence của registry term, **S0 và S1**, full D2L.
2. **Ánh xạ tầng (định nghĩa chặt, phân biệt với "T1/T2/T3" của localizer cũ):**
   - **T1 region** — bọc `region_align` bge-m3 **top-1**; margin (cos#1−cos#2) dưới ngưỡng EV-09 → **top-2**. Ra: câu VI 1:1 cho mỗi occurrence (qua câu EN chứa nó).
   - **T2 code rules** — bọc `occurrence_adherence` JOINT allocation; TRONG câu T1, định tuyến: `C1_primary` (accepted_forms[0]) → credit · `C1_variant` → credit + cờ · `C2plus` (≥2 form cùng term) → đếm + cross-term allocate, khớp → credit · cạnh-tranh-chéo (ứng viên thuộc term track khác) → allocate · `C0` (0 form trong câu) → `not_rendered`(nghi). Phát `resolved_by_tier` + lý do escalate. **Cờ nghi-đội-lốt** = credited active span mà accepted-form KHÔNG xuất hiện trong câu T1 của occurrence.
   - **T3 LLM (GPT)** — bọc máy `localizer_cascade` (LLMClient + json_schema status `{localized, omitted, ambiguous, not_found}` + code-anchor locator position-primary). Chạy **CHỈ trên residual** của T2. `not_found` → **vòng lại T1 nới top-2/3 một lần** → vẫn `not_found` → human.
   - **T4 human** — worksheet (mirror `collision_assignment_gold_review.html`), gán residual + control; **bắt buộc nhãn `not_rendered`**.
3. **4 con số đo (mỗi config):** %T2-resolved · %T3-residual · %human (sau re-narrow) · **đội-lốt = (T2-credited ∧ gold=not_rendered)/T2-credited** — cộng **T3 accuracy** vs gold + **calibration cổng confidence**.
4. **Phân phase (mỗi phase ra số thật, dừng được):**
   - **A** driver với T3 **stub** (trả `escalate`) — chạy được ngay, 0 LLM.
   - **B** chạy T1+T2 full S0/S1 `--tier-max 2` → %T2-resolved + residual count + nghi-đội-lốt count. **0 token, 0$.**
   - **C** T3 GPT `--tier-max 3` trên residual (SAU khi user duyệt prompt) → %dư-LLM-giải vs %đẩy-human + token/occ + $.
   - **D** build residual gold + worksheet; **prefill 23 nhãn tái dùng** từ collision/localizer gold; human gán phần còn lại → score.
   - **E** report 4 số + so S0/S1; gold Phase D → nuôi calibration.
5. **Tái dùng tối đa:** `region_align`, `occurrence_adherence`(allocation), `localizer_cascade`(LLM+anchor), `surface_match`, `embed_cache`, 23 occurrence đã-gán trùng.

**OUT (không lan man):**
- KHÔNG đổi headline `d2l_translate_score.py` / `d2l_translation_metrics_v2.json` (tới khi §6 adopt). Driver ghi `data/reports/cascade_localize_{S0,S1}.json/html` riêng.
- KHÔNG re-translate; frozen DB `mode=ro`, hash bất biến.
- T3 = **chỉ GPT** vòng này (Gemma local so sánh = task sau). KHÔNG model embedding mới; KHÔNG position production.
- KHÔNG SimAlign/word-align (đã loại EV-05/07a). KHÔNG trust number.

## 3. Spec *(Claude viết)*

Files (eval-only; module graduate vào headline CHỈ nếu §6 adopt):
- **NEW `pipeline/eval/cascade_localize.py`** — driver. dataclass `CascadeOccurrence`(occ_id, config, block_id, chapter_id, source_term, term_id, source_span, source_sentence_idx), `TierDecision`(resolved_by ∈ {t2_credit, t2_not_rendered, t3, human}, decision ∈ {rendered<span>, not_rendered, ambiguous}, escalate_reason, masquerade_suspect: bool, t1_sentence, candidates). Hàm: `run_t1_region(...)` (bọc region_align top-1+margin→top-2), `run_t2_rules(...)` (bọc occurrence_adherence allocation + routing C0/C1/C2+ + cờ nghi-đội-lốt), `run_t3(...)` **pluggable**: `t3=None`→stub trả `escalate` (Phase A/B); `t3=LLMAdjudicator`→GPT (Phase C). Lắp record/occurrence + tổng hợp 4 số.
- **NEW `pipeline/eval/llm_adjudicator.py`** (hoặc mở rộng `localizer_cascade`) — T3. Input: câu EN + câu VI + term + ứng viên → schema `{status, target_quote, confidence, reason}`; code-anchor định vị quote (tái dùng locator position-primary). `not_found`→tín hiệu re-narrow.
- **NEW `pipeline/configs/llm_adjudicator.yaml`** — clone `llm_localizer.yaml`: `model: gpt-...` (GPT vòng này), `temperature: 0.0`, `seed`, `reasoning_effort: none` (KHÔNG bật — ăn output budget, `reasoning-effort-consumes-output-budget`), `max_output_tokens`, `prompt_token_cap`, `daily_token_cap`, `pricing`.
- **NEW `pipeline/scripts/run_cascade_localize.py`** — CLI: `--configs S0,S1 --tier-max {2,3} --t3-model gpt --db ... --experiment d2l_p3 --k 3 --out data/reports/cascade_localize_<config>.json`. `--tier-max 2` = Phase B (0 LLM). `--tier-max 3` = Phase C.
- **NEW `pipeline/scripts/build_cascade_residual_gold.py`** + `data/eval/cascade_residual_gold.csv` + worksheet `.html` — Phase D. Liệt kê residual (từ output Phase B/C) + control (mẫu C1_primary đã-resolve + mẫu `a==s`), cột mirror `collision_assignment_gold` + `gold_label{rendered,not_rendered,ambiguous}` **bắt buộc**; **prefill 23 occurrence trùng** từ collision/localizer gold (đối chiếu theo block+source-span); human điền còn lại.
- Tái dùng: `region_align`(T1), `occurrence_adherence`(T2 allocation), `localizer_cascade`(T3 máy+anchor), `surface_match`. Embed cache localhost:1234, gitignored.
- **Param khóa a priori:** T1 bge-m3 top-1, margin gate→top-2 (ngưỡng = lock EV-09; ghi vào report). T3 GPT temp0/seed/reasoning_effort none. nghi-đội-lốt = credited span mà accepted-form không trong câu T1. Driver determinism: cùng input → cùng record (T1 cache + T2 thuần code + T3 replay cache).

**Decision rule (chốt TRƯỚC khi chạy; áp ở §6):**
1. Phase B %T2-resolved per config = mô tả (định cỡ T3), không ngưỡng.
2. T3 chỉ chạy SAU khi user duyệt prompt. **Halt-and-audit** nếu token/call tăng siêu tuyến so số occurrence (`token-growth-halt-and-audit`).
3. **Đội-lốt** = (T2-credited ∧ gold=not_rendered)/T2-credited, S0 vs S1, đo trên gold CÓ `not_rendered` (không phải 102 all-rendered). Đối xứng S0≈S1 → không thổi gap; lệch → báo.
4. T3 accuracy vs gold trên residual + calibration cổng confidence (low-conf ↔ sai?). Cổng KHÔNG calibrate → báo THẾ, không ship cổng.
5. %human = T3 `not_found`(sau re-narrow) + `ambiguous` + low-conf-đã-validate. Mục tiêu tối thiểu hóa; báo số.
DEV chọn thiết kế; KHÔNG trust number. Lock prompt+ngưỡng a priori; held-out riêng. Nếu residual quá nhỏ để tách → báo THẾ thay vì ép verdict.

## 4. Acceptance criteria *(Claude viết — lệnh chạy được)*

```bash
# Phase A/B — 0 LLM, 0$ :
python -m pytest pipeline/tests -k "cascade_localize" -v
#   add: T1 top-1+margin→top-2 wrap; T2 routing C0/C1_primary/C1_variant/C2plus + cross-term;
#        masquerade_suspect flag = credited-but-form-not-in-T1-sentence; T3 stub→escalate; determinism.

python -m pipeline.scripts.run_cascade_localize \
  --configs S0,S1 --tier-max 2 --k 3 \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --embed-endpoint http://localhost:1234/v1/embeddings \
  --models "bge-m3=bge-m3@Q8_0" \
  --out data/reports/cascade_localize
#   -> in per-config: denominator, %T2-resolved (C1_primary/C1_variant/C2plus credit), residual count,
#      masquerade-suspect count; 0 token (in "llm_calls=0"); KHÔNG ghi metrics_v2.

# Phase C — GPT, CHỈ chạy SAU khi user duyệt prompt T3 (gate ngoài lệnh):
python -m pipeline.scripts.run_cascade_localize \
  --configs S0,S1 --tier-max 3 --t3-model gpt --k 3 \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --out data/reports/cascade_localize
#   -> in: %residual T3 giải vs %đẩy-human (sau re-narrow top-2/3), token/occ + $ tổng, daily-cap chưa vượt.
#   warm-cache re-run -> số y hệt, replay-hit 100%.

# Phase D :
python -m pipeline.scripts.build_cascade_residual_gold \
  --from data/reports/cascade_localize_S0.json data/reports/cascade_localize_S1.json \
  --reuse data/eval/collision_assignment_gold.csv data/eval/localizer_gold.csv \
  --out data/eval/cascade_residual_gold.csv
#   -> prefill 23 occurrence trùng; in "to_label = N" (số human phải gán).
# [HUMAN] điền gold_label + gold_target_span cho mọi dòng residual.
python -m pipeline.scripts.run_cascade_localize --score \
  --gold data/eval/cascade_residual_gold.csv --out data/reports/cascade_localize
#   -> FAILS nếu còn dòng chưa gán.
#   -> in: đội-lốt-rate S0/S1, T3 accuracy, calibration (conf-bin × correct), %human.

# Guards (mọi phase):
#   - frozen DB SHA-256 first16 == DA0F687894090D43 (in ra, bất biến).
#   - git diff data/reports/d2l_translation_metrics_v2.json -> EMPTY.
#   - grep -q 'embed_cache' .gitignore -> present.
#   - keys: env-first rồi OPENAI-KEY-*.txt fallback; KHÔNG log key.
python -m pytest pipeline/tests app/backend/tests   # full suite green
```

Report phải đủ để §6 áp decision rule KHÔNG cần chạy lại: per-config denominator + breakdown tầng, residual + masquerade-suspect, (Phase C) token/$/calib, (Phase D) 4 số + T3 accuracy, frozen-DB hash, scope statement.

## 5. Implementation notes *(CodeX điền)*

### 5.1 Code changes

- Added `pipeline/eval/cascade_localize.py`.
  - Enumerates registry-ruler source occurrences from frozen D2L DB (`mode=ro` via sqlite read path).
  - T1 uses `region_align`/LM Studio bge-m3 sentence embeddings, top-1 with margin gate to top-2.
  - T2 uses deterministic accepted-form routing inside the T1 region: `C1_primary`, `C1_variant_flagged`, `C1_variant_shared_with_other_term`, `C2plus_multiple_same_term_forms`, `cross_term_collision`, and `C0_no_accepted_form_in_t1_region`.
  - T3 is a stub in this pass. `--tier-max 3` is deliberately blocked until the prompt/user approval gate is passed.
  - Report includes frozen DB hash, model identity, embedding cache stats, per-config denominator, T2/T3 counts, masquerade suspects, and per-occurrence decisions.
- Added CLI:
  - `pipeline/scripts/run_cascade_localize.py`
  - `pipeline/scripts/build_cascade_residual_gold.py`
- Added `pipeline/configs/llm_adjudicator.yaml` as the gated T3 config stub; no GPT call was made.
- Added `pipeline/eval/llm_adjudicator.py` with review-gated T3 prompt/schema builder and quote validation; still not wired to call GPT in Phase B.
- Added `pipeline/tests/test_cascade_localize.py`.
- Optimized `region_align.EmbeddingCacheClient` with an in-memory cache and batched T1 prewarm in `cascade_localize.py`. Initial naive per-occurrence/per-block embedding calls timed out on full D2L; the final full Phase B run completes.

### 5.2 Phase B run (0 LLM / 0 cloud API)

Command:

```powershell
python -m pipeline.scripts.run_cascade_localize --configs S0,S1 --tier-max 2 --k 3 --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 --embed-endpoint http://localhost:1234/v1/embeddings --models "bge-m3=text-embedding-bge-m3@gpustack/bge-m3-GGUF:Q8_0" --out data/reports/cascade_localize
```

Output files:

- `data/reports/cascade_localize_summary.json`
- `data/reports/cascade_localize_S0.json`
- `data/reports/cascade_localize_S0.html`
- `data/reports/cascade_localize_S1.json`
- `data/reports/cascade_localize_S1.html`

Summary:

| config | denominator | T2 resolved | T3 residual | masquerade suspects | LLM calls |
|---|---:|---:|---:|---:|---:|
| S0 | 8149 | 4786 | 3363 | 115 | 0 |
| S1 | 8149 | 5417 | 2732 | 103 | 0 |

Frozen DB guard:

- `frozen_db_sha256_first16 = DA0F687894090D43`
- `frozen_db_matches_expected = true`

### 5.3 Phase D worksheet generation

Command:

```powershell
python -m pipeline.scripts.build_cascade_residual_gold --from data/reports/cascade_localize_S0.json data/reports/cascade_localize_S1.json --reuse data/eval/collision_assignment_gold.csv data/eval/localizer_gold.csv --out data/eval/cascade_residual_gold.csv
```

Output:

- `data/eval/cascade_residual_gold.csv`
- `data/eval/cascade_residual_gold.html`
- rows: `6095`
- prefilled: `103`
- to_label: `5992`

The high `to_label` count is a real Phase B finding: if every T2 residual goes straight to human, human workload is too large. This supports the intended next step: T3 prompt review + LLM residual adjudication before human review.

Score guard check:

```powershell
python -m pipeline.scripts.run_cascade_localize --score --gold data/eval/cascade_residual_gold.csv --out data/reports/cascade_localize
```

Result: fails as intended because unlabeled residual rows remain. First missing examples include `S0:d2l_introduction_index_b002:registry:first principles:0`, `S0:d2l_introduction_index_b002:registry:problem:0`, `S0:d2l_introduction_index_b002:registry:user:0`.

### 5.4 Verification

```powershell
python -m py_compile pipeline/eval/cascade_localize.py pipeline/scripts/run_cascade_localize.py pipeline/scripts/build_cascade_residual_gold.py
python -m pytest pipeline/tests -k "region_align or cascade_localize" -v
python -m pytest pipeline/tests app/backend/tests
git diff -- THESIS_RUNTIME_TOOL/data/reports/d2l_translation_metrics_v2.json
Select-String -Path '.gitignore' -Pattern 'embed_cache'
```

Results:

- `py_compile`: pass.
- Narrow tests: `10 passed`.
- Full suite: `348 passed in 114.92s`.
- `d2l_translation_metrics_v2.json`: no diff.
- `.gitignore` contains `THESIS_RUNTIME_TOOL/data/eval/embed_cache/`.

### 5.5 Notes for Claude review

- I did not run T3 GPT. `--tier-max 3` exits before any cloud call because the prompt is review-gated by the task.
- Local LM Studio bge-m3 was used for T1 embeddings only. This is local inference through `http://localhost:1234/v1/embeddings`.
- Intermediate probe files from timeout diagnosis were generated under `data/reports/cascade_localize_probe_*`; they are not part of the canonical EV-10 output and can be deleted or ignored during review.

## 6. Review *(Claude điền)*

**Verdict: PASS — Phase A/B machinery + numbers (re-derived độc lập). REWORK `build_residual_gold`+`score` TRƯỚC Phase D (thiếu tầng control credited). Own: spec §2/§3 control-sample của Claude under-target. KHÔNG chặn commit Phase A/B.**

### 6.1 Wiring verified (production == tested path)
- T2 dùng `occurrence_adherence._allocate_source` / `_allocate_target` / `_form_specs` — **JOINT allocation EV-08 nguyên bản, KHÔNG reimplement lệch** ([[occurrence-weighted-block-anymatch-inflation]]). T1 dùng `region_align` bge-m3 (EV-09). `enumerate_occurrences` mirror đúng vòng block của EV-08 (active_source → allocate_target → role active/collision/shadow).
- Tests đi qua `run_t2_rules` THẬT + `AdherenceTerm` thật (không stub allocation). Adjudicator `validate_payload`: quote ngoài region → `not_found` (chặn hallucinate). ([[green-tests-can-hide-dead-integration]] thỏa.)

### 6.2 Số RE-DERIVED từ `decisions[]` trên đĩa (KHÔNG tin báo cáo — [[verify-on-committed-artifacts-not-reports]])
| | denom | t2_credit | residual | masq | partition |
|---|---|---|---|---|---|
| S0 | 8149 | 4786 | 3363 | 115 | credit+residual=denom ✓ |
| S1 | 8149 | 5417 | 2732 | 103 | ✓ |

Breakdown re-derived (khớp tuyệt đối §5): S0 C1_primary 4122 / C0 1023 / C2plus 1016 / **C1_variant_flagged 664** / cross_term_collision 896 / variant_shared 428. S1 C1_primary 5154 / C0 620 / C2plus 1051 / **C1_variant_flagged 263** / collision 946 / variant_shared 115. masq 115/103 đều nằm trong residual (C0_no_form_in_region), 0 trong credit.

### 6.3 Guards (Claude tự chạy)
- Frozen DB SHA first16 = `DA0F687894090D43` (Get-FileHash độc lập) — khớp.
- `git diff metrics_v2.json` rỗng · `.gitignore` có `embed_cache` · 11 targeted tests pass · `--tier-max 3` raise (0 GPT call — cổng prompt giữ).

### 6.4 Findings thật (rơi ra từ Phase B, thuận luận văn)
- **S1 đẩy về form-chính:** C1_primary 5154 vs S0 4122; C1_variant_flagged 263 vs 664; C0 620 vs 1023; variant_shared 115 vs 428 → memory tăng nhất quán canonical (đo được, không suy diễn).
- Residual: **S0 41.3% / S1 33.5%** (S1 code giải nhiều hơn). Cross-sentence masquerade-suspect 115/103.

### 6.5 REWORK trước Phase D (chặn human-label, KHÔNG chặn commit này)
1. **Thiếu tầng control credited.** `build_residual_gold` bỏ MỌI `t2_credit` → worksheet KHÔNG đo được **masquerade cùng-câu được credit**. Tầng at-risk = **C1_variant_flagged: 664 (S0)/263 (S1)** (biến-thể-đơn trong vùng, không share với term track = đúng kiểu user/customer untracked-stealer được credit thẳng, không lên T3). → Thêm control credited (ưu tiên C1_variant_flagged + C1_primary + a==s); mở rộng `score_residual_gold` bắt credited-masquerade (gold=not_rendered HOẶC gold span ≠ credited span). **Nếu không, lặp đúng tội mù của 102 gold cũ.**
   - **Own (Claude):** spec §2/§3 control-sample của mình ghi "C1_primary + a==s" — under-target chính tầng C1_variant_flagged (rủi ro thật). Sửa spec trong task này trước khi re-hand CodeX.
2. **Worksheet mis-scope = "human gold" trên TOÀN residual (6095/to_label 5992).** Theo kiến trúc, residual = **đầu vào T3**, không phải đầu vào human. Human gold = **mẫu phân tầng** của residual (validate T3) + control credited, dựng SAU Phase C. → Đừng gán 5992 bây giờ.

### 6.6 Follow-up (không chặn)
- Rác `cascade_localize_probe_*` (CodeX debug timeout) — KHÔNG commit; có thể xóa.
- Determinism dựa trên frozen embed cache; review không chạy lại full (logic deterministic; cache cố định). Held-out/literary vẫn pending.

### 6.7 Commit scope
Commit: code (`cascade_localize.py`, `llm_adjudicator.py`, 2 script, `llm_adjudicator.yaml`, test), `region_align.py` (memory-cache perf), task §6, LEDGER. **KHÔNG commit** JSON/HTML per-occurrence 19/19/41 MB + residual gold CSV/worksheet (regenerable; worksheet chờ rework) — theo tiền lệ EV-08 (audit lớn regenerable, không commit). Số giữ ở §5/§6.

## REWORK-1 *(Claude spec → CodeX điền §5.6 + STOP, không commit)*

### Part A — Phase C PILOT wiring (ưu tiên; đủ để chạy 20 call)
A1. **Prompt v2** trong `llm_adjudicator.py`: nối thêm vào RULE 5 (bump `PROMPT_VERSION = "d2l_occurrence_adjudicator_v2"`):
```
5. Do not use registry variants as the answer unless they actually appear in
   the target region AND correspond to THIS occurrence. If the only candidate
   is a registry variant that actually translates a DIFFERENT word in the
   sentence, return omitted (not localized).
```
A2. **Mở khoá T3 + nối GPT thật:** bỏ `raise` ở `tier_max>2`. Mỗi residual occurrence → `AdjudicationInput(source_term, source_sentence, target_region = text của T1 ranges, candidate_quotes = surface ứng viên IN-REGION)`. Gọi GPT qua `LLMClient` + `pipeline/configs/llm_adjudicator.yaml` (gpt-5.4-mini, temp0, seed, reasoning_effort none). Áp `validate_payload` (quote phải nằm trong region → nếu không, ép `not_found`). Ghi token in/out + cost mỗi call, cộng dồn.
A3. **`--limit N` + `--only-reused-labeled`:** chạy T3 CHỈ trên residual có nhãn người tái dùng (103 món), cap `--limit`. Pilot mặc định = 20.
A4. **Chấm vs nhãn cũ:** so `status`/`target_quote` của T3 với `gold_label`/`gold_target_span` người → T3 accuracy (localized-correct / omitted-correct / …) + bảng confidence×correct. Ghi `data/reports/cascade_t3_pilot.json`.
A5. **Guards:** honor `daily_token_cap`; key env-first rồi `OPENAI-KEY-*.txt`, **KHÔNG log key**; frozen DB ro hash bất biến; replay cache (re-run = 0 token mới). **Halt-and-audit:** in token/call để Claude kiểm trước khi chạy 83 còn lại.

**Acceptance A (STOP sau output này — user duyệt tiền trước khi chạy nốt):**
```bash
python -m pipeline.scripts.run_cascade_localize --tier-max 3 --t3-model gpt \
  --only-reused-labeled --limit 20 --configs S0,S1 \
  --gold-reuse data/eval/collision_assignment_gold.csv data/eval/localizer_gold.csv \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --out data/reports/cascade_t3_pilot.json
#  -> in: calls, token in/out tổng, cost USD, token/call; T3-vs-human accuracy trên 20.
python -m pytest pipeline/tests -k "cascade_localize or llm_adjudicator" -v   # prompt v2 + validate
```

### Part B — Phase D gold + verify UI (làm SAU pilot)
B1. `build_residual_gold`: **THÊM tầng control credited** — mẫu phân tầng `t2_credit`, **ưu tiên `C1_variant_flagged`** (664 S0/263 S1) + control `C1_primary` + control `a==s`; thêm cột `stratum`. (Sửa control-sample §2/§3; Claude own under-target.)
B2. `score_residual_gold`: mở rộng bắt **credited-masquerade** — false_credit khi `gold_label==not_rendered` HOẶC (`rendered` nhưng `gold_target_span` ≠ span đã credit). Báo tỉ lệ đội-lốt-thật-credited (credited-false/credited) per config, tách khỏi residual-masquerade.
B3. **Worksheet tương tác:** nâng `_write_residual_html` thành bản giống `collision_assignment_gold_review.html` (nút ứng viên, rendered/not_rendered/ambiguous, "dùng bôi đen", Export CSV). Scope = T3-escalations + mẫu control strata (KHÔNG phải cả 6095).

**Acceptance B:** build → html mở được + có strata `C1_variant_flagged` (count>0) + export round-trip; score phát hiện 1 ca credited-not_rendered cấy thử = masquerade.

## 5.6 REWORK-1 implementation notes *(CodeX điền)*

Status: REVIEW (Part A only; STOP, no commit).

### 5.6.1 Code changes
- Updated `pipeline/eval/llm_adjudicator.py` to `PROMPT_VERSION = "d2l_occurrence_adjudicator_v2"` and appended the Rule 5 anti-masquerade sentence exactly as requested.
- Added a separate `run_t3_pilot(...)` path in `pipeline/eval/cascade_localize.py`. It reuses the existing T1/T2 report, selects only residual occurrences with reusable human labels, caps by `--limit`, calls `LLMClient`, validates quotes with `validate_payload`, and writes a single pilot report.
- Added CLI wiring in `pipeline/scripts/run_cascade_localize.py`:
  - `--tier-max 3` requires `--t3-model gpt`, `--only-reused-labeled`, `--limit > 0`, and `--gold-reuse`.
  - Key loading is env-first (`OPENAI_API_KEY`), then file fallback with priority `OPENAI-KEY-2.txt` before `OPENAI-KEY-1.txt`; the key value is never printed.
  - Default LLM config is `pipeline/configs/llm_adjudicator.yaml`; default cache is `data/eval/cascade_t3_llm_cache.sqlite3`.
- Fixed reusable-gold parsing for `collision_assignment_gold.csv`: that file stores `gold_target_span` as `start:end`, not as the quote. The loader now slices the quote from `target_text` before scoring T3. Without this fix, correct LLM quotes were falsely counted wrong.
- Added tests for prompt v2, T3-vs-reused-gold scoring, and collision offset-span conversion.

### 5.6.2 Commands run
```bash
python -m pytest pipeline/tests -k "cascade_localize or llm_adjudicator" -v
```
Result: `7 passed, 210 deselected`.

Fresh pilot command:
```bash
python -m pipeline.scripts.run_cascade_localize --tier-max 3 --t3-model gpt \
  --only-reused-labeled --limit 20 --configs S0,S1 \
  --gold-reuse data/eval/collision_assignment_gold.csv data/eval/localizer_gold.csv \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --out data/reports/cascade_t3_pilot.json
```
Fresh result before the reusable-gold offset fix showed `correct=5/20`, but inspection proved this was a scoring artifact: many gold rows were offset strings like `266:276`, not target quotes.

After fixing gold reuse parsing, reran the same command from cache:
```json
{
  "attempted": 20,
  "correct": 17,
  "accuracy": 0.85,
  "fresh_calls": 0,
  "cache_hits": 20,
  "prompt_tokens_fresh": 0,
  "completion_tokens_fresh": 0,
  "cost_usd_fresh": 0.0
}
```

The first fresh run consumed:
```json
{
  "fresh_calls": 20,
  "prompt_tokens_fresh": 8950,
  "completion_tokens_fresh": 1532,
  "cost_usd_fresh": 0.0053015,
  "usage_today_after": {
    "date": "2026-06-26",
    "total_tokens": 10482,
    "calls": 20
  }
}
```

The report keeps both the replay state and the recorded cached usage:
`prompt_tokens_all_records=8950`, `completion_tokens_all_records=1532`, `cached_recorded_cost_usd=0.0053015`.

### 5.6.3 Remaining pilot errors
All 3 wrong rows are boundary over-extension, not hallucination or invalid quote:
- `S0:d2l_introduction_index_b023:registry:target:0`: gold `mục tiêu`; LLM returned `*mục tiêu*`.
- `S0:d2l_introduction_index_b053:registry:data examples:0`: gold `ví dụ dữ liệu`; LLM returned `với hai ví dụ dữ liệu này`.
- `S0:d2l_preliminaries_index_b003:registry:set:0`: gold `bộ`; LLM returned `một bộ kỹ thuật`.

### 5.6.4 Guards verified
- `--tier-max 3` is still gated by `--only-reused-labeled` + positive `--limit` + `--gold-reuse`.
- Replay cache verified: rerun produced `fresh_calls=0`, `cache_hits=20`, and no new usage_daily increments.
- Frozen DB hash in the pilot report remains `DA0F687894090D43` and `frozen_db_matches_expected=true`.
- No Part B implementation was done.

## 6.8 REWORK-1 review *(Claude điền)*

**Verdict: PASS Part A (pilot wiring). GPT competent ở localization; 0.85 là artifact chấm exact-span, không phải sai nghĩa. Part B (not_rendered gold + verify UI + chuẩn-hóa chấm) là việc kế.**

### 6.8.1 Re-derived độc lập từ `cascade_t3_pilot.json` (20 record trên đĩa)
- accuracy: đếm tay correct=true → **17/20 = 0.85** (khớp report). 3 sai = #5 target, #6 data examples, #16 set.
- **3 sai đều LỆCH BIÊN, không sai nghĩa:** `*mục tiêu*` (kèm dấu `*` markdown) / `với hai ví dụ dữ liệu này` (rộng) / `một bộ kỹ thuật` (rộng) — GPT tìm ĐÚNG từ, chỉ trả span rộng hơn gold. **Đúng-nghĩa ~20/20.**
- **Đội-lốt:** ca b003 `user` (customer→khách hàng cùng đoạn) → GPT chọn **“người dùng”, né “khách hàng”** ✓. Ca `annotation` C0 (code báo vắng-form) → GPT vẫn tìm ra **“chú thích”** ✓ (T3 vá được điểm mù C0 của T2).
- cost: 8950 prompt + 1532 completion → $0.0022375+$0.003064 = **$0.0053015** (tự tính, khớp). **447 token/call** (không bloat). 103→~$0.027, 6095→~$1.6.

### 6.8.2 Guards (Claude tự chạy)
- 7 targeted tests pass · frozen hash `DA0F687894090D43` (Get-FileHash) · **không có chuỗi key thô** trong report (chỉ `api_key_source=file:OPENAI-KEY-2.txt`) · metrics_v2 diff rỗng · replay cache (rerun 0 fresh call).
- Bug CodeX bắt+sửa: `collision_gold` lưu `gold_target_span` dạng offset `266:276` → loader cũ so nhầm; sửa cắt quote từ `target_text` + test. Records giờ hiện quote đúng → fix hợp lý.

### 6.8.3 Giới hạn (phải nói rõ — chưa được tin)
- **Pilot CHỈ kiểm localization.** Cả 103 (và 20) đều `gold_label=rendered` → GPT chỉ phải "tìm chỗ", **CHƯA kiểm khả năng nói `omitted`/`not_found`** = đúng phần phòng-đội-lốt cốt lõi. Cần **gold có not_rendered** (Part B) mới đo được.
- **Confidence vô dụng ở đây:** GPT trả `high` cả 20 (kể cả 3 ca lệch) → cổng confidence chưa tách được đúng/sai. Calibrate phải làm trên gold có not_rendered.
- **Chấm exact-span quá ngặt** → dưới-báo GPT. Trước khi chạy thật: chuẩn hóa (strip `*` markdown + match chứa/minimal-span) HOẶC thêm 1 câu prompt "quote span tối thiểu, bỏ ký tự nhấn mạnh".

### 6.8.4 Commit + next
Commit Part A: code (`cascade_localize.py`, `llm_adjudicator.py`, `run_cascade_localize.py`, test) + `cascade_t3_pilot.json` (39KB, nhỏ — giữ làm bằng chứng pilot) + task §5.6/§6.8 + LEDGER. **Next = Part B** (not_rendered gold + verify UI tương tác + chuẩn-hóa chấm); chạy nốt 83 ca rendered là phụ (thông tin cận biên).

## REWORK-2 *(Claude spec → CodeX điền §5.7 + STOP, không commit)* — SIÊU QUAN TRỌNG đọc kỹ

**THAY ĐỔI THIẾT KẾ (chốt với user 2026-06-27): T3 = LOCATE-ONLY, code chấm.** Lý do: (a) measurement phải NGOÀI model — KHÔNG để LLM tự phán adherent/off_glossary (user bắt lỗi); (b) model chạy `reasoning_effort: none` → bắt làm 1 việc thôi. **Prompt do Claude thiết kế + đã validate (cost $0.0062, kết quả dưới); CodeX DÙNG VERBATIM, KHÔNG sửa/“tối ưu” câu chữ.**

### R2.1 Phân vai (khóa)
- **LLM (T3): CHỈ định vị** cụm VI dịch occurrence này. KHÔNG nhãn adherence, KHÔNG `not_applicable`, KHÔNG phán đúng/sai.
- **CODE: chấm** bằng containment vs `accepted_forms` của Builder: `found=false → not_rendered` · `accepted_form (chuẩn-hóa, strip *) là SUBSTRING của target_quote → adherent` (highlight đúng sub-span code rút) · `else → off_glossary`.
- **Human (T4): verify SPAN** GPT khoanh (không verify điểm).

### R2.2 Prompt T3 — DÙNG NGUYÊN VĂN (system message), bump `PROMPT_VERSION = "d2l_locate_only_v4"`
```
You are an occurrence-level LOCATOR for an English-to-Vietnamese translation evaluation. You do exactly ONE thing: find the Vietnamese text that translates ONE specific occurrence of the marked English term. You do NOT score, judge correctness, or translate anything new.
Input fields:
- SOURCE_SENTENCE: the English sentence.
- TARGET_REGION: the Vietnamese text aligned to that sentence.
- TERM: the English term to locate.
- OCCURRENCE_INDEX: which occurrence of TERM inside SOURCE_SENTENCE to locate (1-based, left to right). Locate exactly that occurrence.
Output:
- found: true if a Vietnamese rendering of this occurrence exists; false if the term is kept in English, dropped, or replaced by a pronoun (then target_quote = "").
- target_quote: the Vietnamese word(s) the translator actually used for this occurrence, copied verbatim from TARGET_REGION. Use whatever the translator wrote, even if it is not the standard term. Stay close to the rendering; include a neighboring word only if needed to copy a contiguous verbatim string. Do not include emphasis markers such as *.
- left_context: the Vietnamese word(s) immediately before target_quote in TARGET_REGION, used to pin the exact position when the same words repeat. "" if none.
- confidence: high, medium, or low - how sure you are of the location.
Rules:
1. Copy target_quote verbatim from TARGET_REGION. Never invent or translate.
2. Honor OCCURRENCE_INDEX. If TERM repeats, do not return a different occurrence.
3. Prefer found = false over guessing a loosely related word.
4. Return only the JSON, no extra fields, no scoring.
```
User message = JSON `{occurrence_id, TERM, OCCURRENCE_INDEX, SOURCE_SENTENCE, TARGET_REGION}` (sort_keys, ensure_ascii=False). **KHÔNG cấp accepted_forms cho LLM** (từ điển ở code). Schema (strict) `{occurrence_id, found:bool, target_quote:str, left_context:str, confidence:enum[high,medium,low]}`. Bỏ status localized/omitted/ambiguous/not_found cũ.

### R2.3 OCCURRENCE_INDEX (code tính)
Số thứ tự occurrence của `source_term` TRONG câu nguồn của nó (1-based, trái→phải), suy từ `source_start` vs offset câu. Validation tạm dùng 1; bản thật phải tính đúng (ca lặp như "bit"×2 mới phân biệt được).

### R2.4 Code scoring + highlight
- `validate`: `target_quote` (nếu found) phải là substring của TARGET_REGION (chuẩn-hóa strip `*`/space) → nếu không, ép `found=false` (chống bịa).
- `score`: dùng matcher của `surface_match`/EV-08 (segment-aware) check accepted_forms ⊂ target_quote. Highlight = sub-span accepted_form code rút (adherent) hoặc cả target_quote (off_glossary). Neo vị trí bằng `left_context`.
- `not_applicable` KHÔNG tồn tại. Polysemy "bit"→"một chút" sẽ ra `off_glossary` (rớt-oan) — **chấp nhận bounded**; fix gốc = Builder scope-down (task khác). Giữ cột `polysemy_suspect` (heuristic: source_term ngắn/phổ thông) để human soi sau, KHÔNG cho LLM phán.

### R2.5 Gold + verify UI (thay Part B cũ)
- Residual gold strata: **C0** + **off_glossary candidates** + **credited control `C1_variant_flagged`** (664/263) + reuse 103; cột `gold_label ∈ {adherent, off_glossary, not_rendered, not_applicable}` + `gold_quote`. Bắt buộc gán; có ca not_rendered/off_glossary thật.
- **Verify UI tương tác** (mirror `collision_assignment_gold_review.html`): hiển thị câu EN + vùng VI + **span LLM khoanh tô màu** + verdict code; nút sửa {adherent/off_glossary/not_rendered/not_applicable} + chọn span + Export CSV. Cờ "khoanh-đúng-nhưng-registry-thiếu-form" (→ vá Builder).
- Score: `adherence_rate = adherent / (adherent+off_glossary+not_rendered)`, loại `not_applicable`; báo **%off_glossary S0 vs S1** (memory tốt → S1 nhỏ hơn) + locate-accuracy (containment) vs human.

### R2.6 Validation Claude đã chạy (locate-only v4, $0.0062, KHÔNG cần CodeX chạy lại phần này)
- 20 ca cũ: code chấm 17/20 adherent; **over-wide PASS** ("hai ví dụ dữ liệu"→rút "ví dụ dữ liệu"; "các tham số của mô hình"→adherent). 2 off_glossary do registry thiếu form (target→mục tiêu, annotation→chú thích) = vấn đề Builder. set→bộ off_glossary đúng.
- C0: predictions→not_rendered ✓ · competition→off_glossary ✓ · dimension→not_rendered ✓ · bit→off_glossary (polysemy bounded) · learning→adherent.
- Bỏ candidate hint → SỬA lỗi cross-term v3.

### Acceptance (R2)
```bash
python -m pytest pipeline/tests -k "cascade_localize or llm_adjudicator" -v
#  add: locate_only_v4 schema {found,target_quote,left_context,confidence}; verbatim PROMPT_VERSION;
#       code containment score (accepted_form substring of quote -> adherent; else off_glossary; not found -> not_rendered);
#       occurrence_index computed; validate forces found=false when quote not in region.
# pilot locate-only on reused-labeled (gated như Part A, user duyệt trước):
python -m pipeline.scripts.run_cascade_localize --tier-max 3 --t3-model gpt \
  --only-reused-labeled --limit 20 --locate-only --configs S0,S1 \
  --gold-reuse data/eval/collision_assignment_gold.csv data/eval/localizer_gold.csv \
  --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 --out data/reports/cascade_t3_locate_pilot.json
#  -> in: locate-accuracy (containment), %adherent/off_glossary/not_rendered, token/$; frozen hash; key không log.
python -m pipeline.scripts.build_cascade_residual_gold --from ... --reuse ... --out data/eval/cascade_residual_gold.csv
#  -> strata gồm C1_variant_flagged>0; verify UI mở được + export round-trip.
```
Guards: frozen DB DA0F687894090D43; metrics_v2 không đổi; embed_cache + llm_cache gitignored; full suite green. STOP, không commit.

## 5.7 REWORK-2 implementation notes *(CodeX điền)*

### 5.7.1 Code changes

- Changed T3 to **locate-only v4** in `pipeline/eval/llm_adjudicator.py`.
  - `PROMPT_VERSION = "d2l_locate_only_v4"`.
  - System prompt is the R2.2 text verbatim.
  - User payload is exactly `{occurrence_id, TERM, OCCURRENCE_INDEX, SOURCE_SENTENCE, TARGET_REGION}` with `sort_keys=True`.
  - No `accepted_forms`, no candidate hints, no adherence labels are sent to the LLM.
  - Output schema is strict locate-only: `{occurrence_id, found, target_quote, left_context, confidence}`.
  - Validation forces `found=false` when the returned quote is not present in the target region.
- Updated `pipeline/eval/cascade_localize.py`.
  - T3 `run_t3_pilot(..., locate_only=True)` now asks the LLM only to locate.
  - CODE computes adherence by containment:
    - `found=false -> not_rendered`
    - accepted Builder form contained in `target_quote -> adherent`, with code-owned highlight subspan
    - otherwise `off_glossary`
  - Added occurrence-index calculation inside the source sentence.
  - Added containment-based scoring against reused human gold, so over-wide but containing quotes are not falsely failed.
  - Added `polysemy_suspect` diagnostic only; it does not decide the label.
- Updated `pipeline/scripts/run_cascade_localize.py`.
  - Added `--locate-only`.
  - `--tier-max 3` now requires the R2 gate flags: `--locate-only`, `--t3-model gpt`, `--only-reused-labeled`, `--limit`, and `--gold-reuse`.
- Updated `pipeline/scripts/build_cascade_residual_gold.py` through the shared builder.
  - Worksheet now includes residual strata plus credited-control stratum `control:C1_variant_flagged`.
  - CSV fields include `stratum`, `accepted_forms`, `credited_target_surface`, `gold_quote`, and `registry_missing_form_flag`.
  - HTML worksheet supports interactive text selection, label buttons, registry-missing-form toggle, and CSV export.
- Updated tests in `pipeline/tests/test_cascade_localize.py`.
  - Checks locate-only prompt/schema and verifies no accepted forms leak into the LLM prompt.
  - Checks code containment scoring (`target_quote` wider than canonical form still credits the contained form).
  - Checks off-glossary classification when the LLM locates a real but non-registry rendering.
  - Checks source-sentence `OCCURRENCE_INDEX` computation.

### 5.7.2 Verification commands

```powershell
python -m pytest pipeline/tests -k "cascade_localize or llm_adjudicator" -v
python -m pipeline.scripts.run_cascade_localize --tier-max 3 --t3-model gpt --only-reused-labeled --limit 20 --locate-only --configs S0,S1 --gold-reuse data/eval/collision_assignment_gold.csv data/eval/localizer_gold.csv --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 --out data/reports/cascade_t3_locate_pilot.json
python -m pipeline.scripts.build_cascade_residual_gold --from data/reports/cascade_localize_S0.json data/reports/cascade_localize_S1.json --reuse data/eval/collision_assignment_gold.csv data/eval/localizer_gold.csv --out data/eval/cascade_residual_gold.csv
```

Results:

- Targeted tests: `9 passed, 210 deselected`.
- Full suite: `352 passed in 118.98s`.
- Fresh locate-only pilot was run once:
  - `attempted=20`
  - `correct=20`
  - `accuracy=1.0` by containment against reused human gold
  - `adherence_counts={"adherent":17,"off_glossary":3}`
  - S0 pilot subset: `adherence_rate=0.85`, `off_glossary_pct=0.15`
  - fresh usage: `prompt_tokens=11125`, `completion_tokens=1184`, `cost_usd=0.00514925`
  - OpenAI usage after run: UTC date `2026-06-26`, `total_tokens=60130`, `calls=99`
- Warm replay was run immediately after:
  - current `data/reports/cascade_t3_locate_pilot.json` shows `fresh_calls=0`, `cache_hits=20`, `cost_usd_fresh=0.0`
  - the same report keeps `prompt_tokens_all_records=11125`, `completion_tokens_all_records=1184`, and `cached_recorded_cost_usd=0.00514925`
- Frozen DB guard in the report:
  - `frozen_db_sha256_first16 = DA0F687894090D43`
  - `frozen_db_matches_expected = true`
- `git diff -- THESIS_RUNTIME_TOOL/data/reports/d2l_translation_metrics_v2.json` is empty.
- Cache guard:
  - embed cache is explicitly gitignored.
  - the T3 SQLite cache path `data/eval/cascade_t3_llm_cache.sqlite3` is covered by the repo-wide `*.sqlite3` ignore rule.

### 5.7.3 Residual gold worksheet

Generated:

- `data/eval/cascade_residual_gold.csv`
- `data/eval/cascade_residual_gold.html`

Counts:

| stratum | rows |
|---|---:|
| residual:C0 | 1643 |
| residual:C2plus_multiple_same_term_forms | 2067 |
| control:C1_variant_flagged | 927 |
| residual:cross_term_collision | 1842 |
| residual:off_glossary_candidate | 543 |
| **total** | **7022** |

Label state:

- `gold_label` prefilled: `142`
- unlabeled: `6880`
- prefilled labels: `139 adherent`, `3 off_glossary`

HTML checks:

- contains `Export CSV`
- contains `Use selection`
- contains `registry missing form`
- contains all R2 labels: `adherent`, `off_glossary`, `not_rendered`, `not_applicable`

### 5.7.4 Notes / caveats for Claude review

- I did not commit.
- The pilot report is currently a **warm replay artifact**, not the first fresh-call artifact. It is still useful because it proves replay/caching and preserves recorded token/cost totals from the first run.
- The worksheet is large because R2 intentionally adds credited controls (`C1_variant_flagged`) and all residual strata. This is expected for audit, but it should not be treated as "all rows must be human-labeled now"; it is an input artifact for Part B sampling/verification policy.

## 6.9 REWORK-2 review *(Claude điền)*

**Verdict: PASS. Locate-only + code-score = đúng thiết kế (measurement NGOÀI model); prompt VERBATIM; số re-derived khớp. Worksheet sửa lỗ REWORK-1. Pilot vẫn chỉ kiểm localization → đo hard-set là việc kế (human label strata).**

### 6.9.1 Prompt VERBATIM (yêu cầu cứng của user) — ĐẠT
`llm_adjudicator.py:42-59` khớp **từng chữ** prompt `d2l_locate_only_v4` Claude thiết kế (system + 4 input + 3 output + 4 rule). Payload `{occurrence_id,TERM,OCCURRENCE_INDEX,SOURCE_SENTENCE,TARGET_REGION}` sort_keys; schema `{found,target_quote,left_context,confidence}`. CodeX KHÔNG sửa câu chữ. ✓

### 6.9.2 Measurement NGOÀI model — ĐẠT
`_score_locate_only_by_code` (casc:899) **deterministic**: found=false→not_rendered; `find_spans(quote, accepted_form, vi)` (matcher EV-08, segment-aware) có→**adherent** (highlight sub-span) else→**off_glossary**. **LLM không phát nhãn adherence.** `polysemy_suspect` = heuristic CODE. `validate_payload` ép `found=false` khi quote ngoài region (chống bịa). Đúng phân vai: LLM khoanh · code chấm.

### 6.9.3 Số RE-DERIVED từ `cascade_t3_locate_pilot.json` (KHÔNG tin báo cáo)
- **Locate 20/20** (containment vs gold cũ) · code **17 adherent / 3 off_glossary**.
- 3 off_glossary: `target`→"mục tiêu", `annotation`→"chú thích" (= **registry THIẾU form**, vấn đề Builder, không phải T3), `set`→"một bộ kỹ thuật" (off-glossary thật). → LLM khoanh ĐÚNG cả 20, code phán adherence độc lập.
- **Over-wide HẾT:** "hai ví dụ dữ liệu"/"các tham số của mô hình" → adherent qua find_spans containment.
- cost = 11125×$0.25/M + 1184×$2/M = **$0.00515** (tự tính khớp); pilot là warm-replay (0 fresh, 20 cache hit) — token/cost của lần fresh đầu được giữ.

### 6.9.4 Worksheet (sửa lỗ REWORK-1) + verify UI
7022 rows, strata: **`control:C1_variant_flagged`=927** (= 664 S0+263 S1, đúng tầng credited mình yêu cầu) + `off_glossary_candidate`=543 + C0 1643 + C2plus 2067 + collision 1842. Verify UI có nút {adherent/off_glossary/not_rendered/**not_applicable**} + chọn span + export. Human VẪN gán được `not_applicable` (đo polysemy/đối xứng) dù LLM không phán — nhất quán.

### 6.9.5 Guards (Claude tự chạy)
9 targeted pass · frozen hash `DA0F687894090D43` (Get-FileHash) · metrics_v2 diff rỗng · llm_cache ignored qua `*.sqlite3` · key chỉ ghi `file:OPENAI-KEY-2.txt`, không lộ · CodeX full 352 pass.

### 6.9.6 Chưa được tin (việc kế)
- Pilot vẫn **localization-only** (103 reused toàn rendered) → khả năng bắt off_glossary/not_rendered trên **hard-set CHƯA đo** = phần human label strata (Part D thật).
- 2/3 off_glossary là **registry thiếu form** → verify-UI nên cờ "khoanh-đúng-nhưng-registry-thiếu-form" để vá Builder (đã có trong spec, kiểm khi human dùng).
- `OCCURRENCE_INDEX` ca lặp thật (bit×2) chưa exercise; confidence chưa calibrate.

### 6.9.7 Commit
Commit: code (`cascade_localize.py`, `llm_adjudicator.py`, `run_cascade_localize.py`, test) + `cascade_t3_locate_pilot.json` (46KB evidence) + task §5.7/§6.9 + LEDGER. **KHÔNG commit** worksheet CSV/HTML 8.6MB (regenerable, chờ label) + big cascade_localize_*.json.
