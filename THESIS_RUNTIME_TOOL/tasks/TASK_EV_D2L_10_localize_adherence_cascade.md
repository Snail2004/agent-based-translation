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
