# TASK BUILDER-V2 — Builder D2L v2: trích độc lập (recall) + sổ-tay-có-lọc (memory-pack) + code consolidation là QUYỀN CUỐI

Status: Stage A+B+C1 PASS; **Stage C2 real-run PASS (Claude §21, re-derived)** — v2 THẮNG cấu trúc (recall 0.632→0.667, entries 381→340, number-dup 29→0, cost $0.131, 0 parse-fail, frozen ro). "Agreement tụt 0.806→0.605" = ~0.08 thiên-lệch-thước (v1 variant-bloat 4.36 vs 1.71) + ~0.12 thật nhưng ca lệch hầu hết ĐỒNG NGHĨA hợp lệ → **chất lượng dịch CHƯA kết luận, cần judge mù**. Next: judge mù ~15 ca lệch; soi 3 miss; (Phase D migration). **Stage C3 spec READY (§22, Claude)** — Term-Auditor tầng 2 LLM lọc trích-dư (340 còn ~46% xuất-hiện-1-lần); kiến trúc 2 tác nhân (Builder recall + Auditor precision) + code chỉ cơ học (gỡ stoplist hardcode); là GIẢ-THUYẾT cần đo (pass = precision↑ & recall không dưới sàn ≥0.632). Model auditor = biến mở (chưa CM mini đủ thẩm định).
Type: BUILDER redesign + method-decision. Builder **MÙ với gold D2L** (eval-only). KHÔNG đổi production `glossary_entries` tới Phase D. Pilot ghi **artifact JSON**, KHÔNG ghi DB. Frozen DB `mode=ro`.

- **Refs (đã verify trên file thật session này):** prepass hiện tại — `prompt.py` `d2l_terminology_v7` (registry TẮT: `D2L_REGISTRY_OMITTED_TEXT`) · `registry.py:merge` key=`source_term.casefold()` · `persist.py:_persist_glossary` dòng 301/318 cũng casefold · `span_resolver._find_word_boundary_matches(text, source_term)` match **đúng 1 surface** (regex `\b…\b`) · `glossary_entries` **CHƯA có** `source_variants_json` · `context_builder.plan_anchors` (mẫu anchor đang dùng cho Translator) · `builder_gold.score_builder_vs_gold` (eval vs D2L gold). Memory: prompt-memory-design-is-first-class, builder-v2-memory-pack-design, dont-tune-intervention-on-test-baseline, scoring-scope-equals-production-scope, token-growth-halt-and-audit, green-tests-can-hide-dead-integration, four-tier-localize-cascade-locked.
- **Branch/Commit:** (điền khi imple)

## 1. Bối cảnh & mục tiêu *(Claude)*

Builder D2L hiện tại trích **mù** (prompt cấm xem registry-so-far → `D2L_REGISTRY_OMITTED_TEXT`) rồi gộp bằng code theo **mặt chữ** (`casefold`) ở CẢ `registry.merge` lẫn `persist._persist_glossary`. Hệ quả đo được: **1608 term**, **353 từ một-âm-tiết phổ thông** (features/models/inputs/weights…), và **số-ít/số-nhiều bị tách đôi** (`feature`+`features`, `model`+`models`) vì gộp theo surface.

**Builder v2 = 3 LỚP** (không bỏ cái cũ, thêm trí nhớ CÓ KIỂM SOÁT):
1. **L1 — Trích độc lập mỗi window:** giữ RECALL cao (không cap số term lúc build — recall-khi-build, precision-khi-inject).
2. **L2 — Sổ-tay-có-lọc (memory-pack):** code quét window, chỉ bơm entry registry-so-far **có surface trong window** + `near_number_variants`. KHÔNG full dump (full dump = lỗi cũ nổ quota). Để Builder **nối/bổ sung** thay vì tạo trùng.
3. **L3 — Code consolidation = QUYỀN CUỐI + audit:** gộp số-ít/số-nhiều deterministic, sum occurrences, giữ mọi source surface, apply update, **flag conflict**. **LLM chỉ ĐỀ XUẤT, không tự gộp / không tự đổi canonical** (LLM phụ thuộc thứ tự window+prompt → hỏng tái lập).

**Mục tiêu đo:** v2 so baseline trích-mù — (a) entry giảm? (b) recall-vs-gold có tụt? (c) conflict theo loại? (d) token/window ổn? (e) **occurrence-evidence có bị mất?**

## 2. Scope

**IN:** helper `concept_key`; L2 pack-builder + audit; prompt v8 + schema 4 rổ; L3 consolidation; 6 mục thiết kế render offline; phases A→D.

**OUT (không lan man):**
- KHÔNG đổi production `glossary_entries` / headline tới Phase D. Pilot ghi artifact JSON riêng.
- KHÔNG bơm gold D2L vào prompt. Gold chỉ để CHẤM pilot (recall/precision), pilot-chương là **DEV**.
- KHÔNG full dump registry. KHÔNG để LLM tự đổi canonical. KHÔNG stemming rộng (chỉ số-ít/số-nhiều).
- Frozen DB `mode=ro`, hash bất biến.

## 3. Thiết kế *(Claude)*

### 3.1 Helper `pipeline/prepass/concept_key.py` (số-only, bảo thủ)
`concept_key(phrase)` thứ tự: NFC+casefold+trim+collapse-space → **phrase-override TRƯỚC** (`CONCEPT_KEY_OVERRIDES`/`DONT_SINGULARIZE_PHRASES`: least squares, ordinary least squares, naive bayes…) → else singularize từng token → ghép.
`singularize_token(t)` thứ tự: (1) `t∈DONT_SINGULARIZE_TOKENS`→giữ (loss, bias, axis, basis, analysis, hypothesis, synthesis, diagnosis, series, species, status, lens, news, mathematics, statistics, physics, corpus, bus, gas; **logits**=GẮN CỜ audit) · (2) `endswith("ss")`→giữ (class, loss, process) · (3) `t∈IRREGULAR_PLURALS`(dict explicit: axes→axis, analyses→analysis, hypotheses→hypothesis, matrices→matrix, indices→index, vertices→vertex) — **TRƯỚC** regular · (4) `len≤3`→giữ · (5) regular: `-ies→y` · `-es`(gốc s/x/z/ch/sh)→bỏ es · `-s`(không `ss`)→bỏ s.
**Cấm gộp phái sinh:** train/training, general/generalization, compute/computation — chỉ NUMBER (CodeX điểm 6). Pure + test từng nhánh.

### 3.2 L2 memory-pack builder (code, trước khi gọi LLM)
Quét surface trong window, tra registry-so-far, bơm pack nhỏ **chỉ gồm**: `matched_existing_terms` (entry có surface trong window — canonical + 1–2 biến thể) · `near_number_variants` (window `features`, registry `feature` qua `concept_key`). Có **trần token**. Tái dùng mẫu `context_builder.plan_anchors`.
**Audit BẮT BUỘC (CodeX điểm 2):** `included_by_exact_surface`, `included_by_concept_key`, `excluded_no_surface_match`, `dropped_by_budget`, `pack_token_estimate`, `window_term_surfaces_detected`. *(Để biết pack nhỏ vì THÔNG MINH hay vì BỎ SÓT.)*

### 3.3 Prompt v8 + schema 4 rổ + guard mất-recall
Schema output tách 4 rổ: `new_terms` · `updates_to_existing` (thêm source_variant/target_variant/evidence) · `conflicts` (muốn ĐỔI canonical — phải khai, không âm thầm) · `seen_existing_terms` (term cũ trong window, không đổi — liệt kê để giữ evidence).
**🔒 Guard mất-recall (CodeX điểm 3, ghi NGUYÊN VĂN trong prompt):**
> "Every source term occurrence in this window must be represented exactly once across the four buckets. Existing terms are not exempt. If an existing term appears but needs no change, put it in `seen_existing_terms` with evidence block ids."

### 3.4 `updates_to_existing` — chống variant-bloat (CodeX điểm 4)
- Chỉ thêm target-variant nếu **xuất hiện trong evidence** hoặc model giải thích ngắn; mọi variant mới **phải có `evidence_block_id`**.
- KHÔNG thêm biến thể chỉ khác `các/những` nếu đã normalize được.
- **Giới hạn số variant mới mỗi term/window.**

### 3.5 `conflicts` — phải có LOẠI (CodeX điểm 5)
`canonical_target_change` · `polysemy_suspected` · `bad_existing_target` · `plural_only_difference` · `uncertain`.

### 3.6 L3 consolidation = QUYỀN CUỐI (code)
`new_terms`→tạo entry (sau number-merge). `updates_to_existing`→union source/target variants (có kiểm soát, **không lấy bản dính các/những làm canonical**), cộng evidence/occurrence. `conflicts`→**ghi audit, KHÔNG tự đổi canonical** (người xem quyết). `seen_existing_terms`→chỉ cộng occurrence/evidence. Number-merge qua `concept_key`; giữ mọi surface ở `source_variants_json` (Phase D). **LLM không phải nguồn quyết định gộp.**

## 4. 6 mục thiết kế bắt buộc — render OFFLINE trước mọi run *(deliverable Phase B)*
1. **Prompt mẫu thật** trên 1 window thật (vd `preliminaries`) kèm pack thật. 2. **Chính sách context** (trong: matched+near_number; ngoài: còn lại). 3. **Ngân sách token** (system/pack/source/output; ước tính/window + tổng 1 chương). 4. **Cache** (prefix ổn định=system+schema; suffix đổi=window+pack; cache-key). 5. **Điều kiện dừng** (halt nếu pack/prompt vượt ngưỡng — token-growth-halt-and-audit). 6. **Báo cáo cost-quality** (token/window, $/chương, + chất lượng).

## 5. Lộ trình A/B/C/D *(CodeX, Claude đồng ý)* — mỗi stage tự ra số, dừng được
- **BUILDER-V2-A** — number-merge **offline probe** trên registry cũ. **0 API, 0 DB write.** `concept_key` + probe report (JSON+CSV: concept_key, source_terms, targets, occurrence_sum, merge_reason, risk_flags, target_conflict_type) + rematch-proof + tests. Ra `1608→N`, audit merge pairs, over-merge risk. *(KHÔNG dùng gold — CodeX điểm 7.)*
- **BUILDER-V2-B** — **render-only** memory-pack + prompt schema. **0 API.** Xuất prompt thật + pack-audit + token estimate (6 mục §4). Chưa gọi LLM.
- **BUILDER-V2-C** — **pilot 1 chương** vào **artifact JSON / temp** (CÓ source variants trong artifact, **KHÔNG ghi `glossary_entries`** — CodeX điểm 1). API có **cost-gate** + stop-condition.
- **BUILDER-V2-D** — chỉ nếu pilot ổn: migration `source_variants_json` + update consumers (registry, persist, span_resolver match theo mọi surface, occurrence_adherence, d2l_translate_score, thesis_overlay, context_builder; **fallback `[source_term]` khi cột thiếu/rỗng** — backward-compat) + full run.

## 6. Baseline & metrics *(CodeX điểm 7+8)*
- **Baseline = Builder cũ v7** (registry hiện tại trong frozen DB) **trên cùng chương** pilot. *(Không phải re-run cache mơ hồ.)*
- **Pilot-chương = DEV**, KHÔNG phải headline. Nếu chỉnh prompt/consolidation dựa pilot → số đó **không** được làm headline; benchmark = **held-out hoặc 1-lần-sau-freeze**.
- Metrics: entry count · **recall-vs-gold (dev, eval-only)** · conflict rate theo loại · token/window · **occurrence-evidence giữ được** (so baseline).

## 7. Guards / 5 cổng nghiệm thu / lằn ranh eval
**5 cổng (CodeX):** không full dump · LLM không tự sửa canonical âm thầm · schema tách new/update/conflict/seen · code consolidation là lớp cuối · đo cost+recall TRƯỚC khi chạy lớn.
**Lằn ranh:** Builder mù với gold D2L; gold chỉ chấm pilot. **L3 phải có test wiring thật**, không chỉ stub (green-tests-can-hide-dead-integration). Bump `prompt_version` khi đổi bytes.

## 8. Acceptance *(lệnh chạy được — §4 LEDGER)*
- A: `python -m pytest pipeline/tests/test_concept_key.py -q` (xanh; cover DONT/irregular/regular/phrase + cấm train↛) **và** `python pipeline/scripts/builder_concept_probe.py --db data/jobs/d2l_p1/memory.sqlite3 --out data/reports/builder_v2_a_probe` → in `1608→N` + JSON+CSV + rematch-proof; 0 DB write (`git status` sạch DB).
- B: `python pipeline/scripts/builder_v2_render.py --chapter preliminaries --dry-run --out data/reports/builder_v2_b_render` → prompt thật + pack-audit (6 trường §3.2) + token estimate; **0 API call** (assert trong test).
- C: `python pipeline/scripts/builder_v2_pilot.py --chapter preliminaries --artifact data/reports/builder_v2_c_pilot.json --cost-cap <token>` → artifact có new/update/conflict/seen + source_variants; **không** ghi `glossary_entries`; báo 5 metrics §6.
- D: (điền sau khi C PASS) migration + `python -m pytest` full xanh + re-run + so baseline.

## 9. §5 — CodeX implementation notes *(CodeX; STOP, không commit)*

### Stage A implemented only

Implemented **BUILDER-V2-A** only:

- `pipeline/prepass/concept_key.py`
  - Conservative number-only `concept_key()`.
  - Phrase overrides: `least squares`, `ordinary least squares`, `naive bayes`, `naive bayes classifier`.
  - DONT tokens + irregular plural whitelist.
  - No derivational stemming (`train` != `training`, `compute` != `computation`, `general` != `generalization`).
- `pipeline/scripts/builder_concept_probe.py`
  - Opens DB with SQLite `mode=ro`.
  - Reads existing `glossary_entries` only; does **not** read `eval_glossary_gold`.
  - Groups registry rows by `concept_key`.
  - Writes JSON + CSV reports.
  - Computes DB SHA-256 before/after and raises if changed.
  - Rematch-proof uses the same source matcher as current Builder (`span_resolver._find_word_boundary_matches`), not `surface_match.find_spans`, because `surface_match` masks code/URL and would create false mismatches against the legacy Builder count.
- `pipeline/tests/test_concept_key.py`
  - Covers regular plurals, DONT tokens, irregular whitelist, phrase overrides, derivational non-merge, merge reason.

### Commands run

```powershell
cd C:\work\odl-pdf-demo\research\agent-based-translation\THESIS_RUNTIME_TOOL
python -m pytest pipeline/tests/test_concept_key.py -q
python pipeline/scripts/builder_concept_probe.py --db data/jobs/d2l_p1/memory.sqlite3 --out data/reports/builder_v2_a_probe
python -m py_compile pipeline\scripts\builder_concept_probe.py pipeline\prepass\concept_key.py
```

Results:

- `test_concept_key.py`: **6 passed**.
- Probe: **0 API**, DB opened read-only, DB hash unchanged.
- Frozen DB hash: `da0f687894090d43b75a3ae52ba71ec1edf85dab3198c9f86039879365d464b8`.

### Stage A output

Artifacts:

- `data/reports/builder_v2_a_probe/builder_v2_a_probe.json`
- `data/reports/builder_v2_a_probe/builder_v2_a_groups.csv`
- `data/reports/builder_v2_a_probe/builder_v2_a_merged_groups.csv`

Headline Stage A numbers:

| Field | Value |
|---|---:|
| raw_terms | 1608 |
| virtual_terms after number-merge | 1486 |
| merged_groups | 122 |
| merged_terms_removed | 122 |
| common_short_before | 244 |
| common_short_after | 224 |
| occurrence_sum_before | 13252 |
| occurrence_sum_after | 13252 |
| rematch_mismatch_groups | 0 |

Target conflict counts after virtual merge:

```json
{
  "none": 1457,
  "target_divergence": 24,
  "plural_marker_only": 5
}
```

Risk flag highlights:

- `number_variant`: 440 groups/items flagged.
- `target_divergence`: 29 flagged groups.
- `common_short_source`: 218 flagged groups.
- irregular merges observed: `axes->axis`, `matrices->matrix`, `indices->index`.

High-impact safe-looking merge examples:

- `model/models`: 478 occurrences, target `mô hình`.
- `example/examples`: 259 occurrences, target `mẫu`.
- `layer/layers`: 193 occurrences, target `lớp`.
- `parameter/parameters`: 174 occurrences, target `tham số`.
- `feature/features`: 127 occurrences, target `đặc trưng`.

Conflict examples requiring review before Phase B:

- `dataset/datasets`: `bộ dữ liệu` vs `tập dữ liệu`, 157 occurrences.
- `loss/losses`: `hàm mất mát` vs `các giá trị mất mát`, 153 occurrences.
- `activation/activations`: `kích hoạt` vs `giá trị kích hoạt`, 39 occurrences.
- `data example/data examples`: `ví dụ dữ liệu` vs `mẫu dữ liệu`, 25 occurrences.
- `ground-truth label/ground-truth labels`: `nhãn chân lý cơ sở` vs `nhãn chuẩn`, 3 occurrences.

### CodeX interpretation

Stage A supports the claim that number-variant duplication is real but bounded:

- The current 1608-entry registry would shrink by **122 entries** with conservative number-merge.
- Occurrence evidence is conserved under the legacy Builder matcher (`13252 -> 13252`, rematch mismatches `0`).
- This does **not** solve over-extraction by itself: common-short diagnostic only drops `244 -> 224`.
- Phase B should not blindly auto-merge all groups: at least **29 merged groups** need target-conflict handling/audit.

Recommendation: Claude can review Stage A artifacts now. If Phase B proceeds, it must include `source_variants_json` and consumer fallback `[source_term]`; otherwise the merged registry will look cleaner while losing source occurrence surfaces.

## 10. §6 — Claude review *(Claude, 2026-06-29)*

**Verdict: Stage A PASS.** Re-derive ĐỘC LẬP (grouping riêng trên DB, KHÔNG qua probe CodeX) — khớp CHÍNH XÁC §5:
- 1608 → **1486** (−122); merged groups **122**; occurrence **13252 → 13252** (bảo toàn); conflict target **29** (24 target_divergence + 5 plural-only).
- `concept_key` adversarial (Claude tự test): `train`≠`training`, `general`≠`generalization`, `compute`≠`computation` (KHÔNG gộp phái sinh ✓); `axes→axis`/`analyses→analysis`/`matrices→matrix`/`indices→index` (irregular ✓); `biases→bias`/`classes→class`/`features→feature`/`probabilities→probability` ✓; `bias`/`class`/`loss`/`analysis`/`logits` giữ nguyên ✓.
- `test_concept_key.py` re-run = **6 passed**. Frozen DB `DA0F687894090D43` khớp; `data/jobs/` sạch (0 DB write); 0 API.

**Phát hiện (giá trị thật của Stage A):**
1. Number-merge **đúng + an toàn** nhưng **khiêm tốn** (−122/1608 ≈ 7.6%); phần phình lớn = over-extraction từ phổ thông **CHƯA đụng** (common-short 244→224, mà đây chỉ là proxy thô ≤7 ký tự, KHÔNG phải thước termhood). → over-extraction là việc của L2/prompt (Stage B/C).
2. **29 conflict = bằng chứng Builder dịch KHÔNG nhất quán giữa window** (`dataset` bộ/tập · `target` biến/nhãn · `minibatch` dịch/giữ-Anh · `pixel` pixel/điểm-ảnh) → **củng cố hướng L2 memory-pack**. Dùng list này làm **fixture đo L2** (pack có giảm conflict không).
3. **2 ca đa nghĩa, gộp có thể SAI:** `loss` = "các giá trị mất mát"(loss-values) vs "hàm mất mát"(loss-function); `score` = chấm điểm(động từ) vs điểm số(danh từ). → Phase B `concept_key` cần cờ `sense_conflict` (KHÔNG auto-merge 2 ca này; chờ người xác nhận). KHÔNG phải blocker Stage A.

**Phase B (điều kiện cứng, xác nhận lại):** `source_variants_json` + consumer fallback `[source_term]`; KHÔNG blind-merge canonical VI (29 nhóm); xử lý `sense_conflict`.

**Next:** Stage A đóng. Đề xuất sang **Stage B** (render-only memory-pack + prompt v8, 0 API) — vừa tấn công over-extraction vừa chặn conflict tại gốc; dùng 29 conflict + 122 merge làm fixture.

**Commit:** Stage A code (`concept_key.py` + probe + test) + task §6 + LEDGER. Artifact `builder_v2_a_probe/` (984KB JSON regenerable) → gitignore.

## 11. Stage B — Render-only memory-pack + prompt v8 *(Claude spec; prompt VERBATIM)*

**Mục tiêu:** chứng minh cơ chế sổ-tay + prompt **trên giấy** (prompt thật, token thật, audit thật) TRƯỚC khi gọi LLM ở Stage C. **0 API, 0 DB write.** Prompt do Claude sở hữu; CodeX dùng **nguyên byte**; bump version khi đổi byte.

### B.1 — L2 pack-builder (code)
Input: 1 window (list block) + sổ-tay registry-so-far. Output: pack nhỏ + audit.
Pack chỉ gồm: `matched_existing_terms` (entry có source-surface trong window; canonical + ≤2 biến thể VI) · `near_number_variants` (window `features` ↔ registry `feature` qua `concept_key` Stage A).
**Ngưỡng CỨNG:** `PACK_TOKEN_CAP=1500`, `PROMPT_TOKEN_CAP=6000` (halt nếu vượt).
**Deterministic:** sort `(match_type, source_term, concept_key, glossary_id, block_id)`; JSON `separators` ổn định → cache + diff sạch; chạy 2 lần ra byte y hệt.
**Audit bắt buộc (8 trường):** `included_by_exact_surface`, `included_by_concept_key`, `excluded_no_surface_match`, `dropped_by_budget` (kèm `priority`+`reason`, không chỉ list), `pack_token_estimate`, `window_term_surfaces_detected`, `pack_source_mode`, `pack_provenance`.
**2 chế độ `--pack-mode`:**
- `proxy_full_registry` — dùng full registry v1 làm notebook (stress-test token; CÓ THỂ thấy term từ block sau — ghi rõ).
- `proxy_chronological` — chỉ include entry có **evidence-block trước window hiện tại**: lọc bằng `glossary_entries.evidence_span_ids_json` ↔ `blocks.order_index` (chặn future-leak kiểu preview TI). *(Schema đã đủ dữ liệu — verified.)*

### B.2 — PROMPT `d2l_terminology_v8` (Claude thiết kế, CodeX VERBATIM)

SYSTEM:
```
You are the World Builder agent for an autonomous English→Vietnamese technical-book
translation pipeline (D2L). Read ONLY the English source window provided. Maintain a
terminology registry consistent across the whole book. Never use any Vietnamese
reference, glossary, gold, or answer key — build from the English source and YOUR OWN
prior notes only.

INPUTS:
- ENGLISH_SOURCE_WINDOW: source blocks with [block_id] markers.
- MEMORY_PACK: terms YOU already coined in earlier windows that also appear in this
  window (YOUR OWN notebook — a continuity aid, NOT an answer key). Each item:
  source_term, canonical_target_vi, allowed_variants[], and for near-number items the
  related surface seen in this window.

JOB: account for every controlled term/concept visible in this window by placing it in
EXACTLY ONE of four buckets. Favour RECALL — extract generously; a downstream
deterministic filter (NOT you) decides which terms are consistency-bearing.

Hard rules:
- Prompt version: d2l_terminology_v8. Return ONLY valid JSON matching the contract.
  Keep strings concise; no commentary outside JSON.
- A controlled term needs book-wide consistency: ML concepts, math/statistics terms,
  model/layer/architecture names, abbreviations, framework/API names, named
  datasets/algorithms.
- New-term restraint (applies to `new_terms` ONLY): by default do NOT create a NEW
  standalone entry for an ordinary English word (input, output, value, number, result,
  example, sample, set, case, problem, step, size). DO create one when the word is used
  as a controlled ML/math concept, is repeated as a concept across evidence blocks,
  appears in a definition/heading/math context, or is already in MEMORY_PACK. When a
  precise multi-word term covers the concept ("input layer", "loss function", "feature
  map"), emit that and do not also emit the bare head as a separate new term.
- Existing MEMORY_PACK terms are NEVER subject to that restraint: they must always be
  accounted (see RECALL RULE). If you think a pack term is too generic to be a real term,
  report it in `conflicts` with conflict_type "termhood_suspected" — never drop it
  silently.
- Prefer ONE canonical source surface per concept, singular base form. Record number
  variants ("features" vs "feature") as updates_to_existing, not as new terms.
- Each new term commits to ONE canonical Vietnamese target with FULL diacritics
  ("tác nhân", not "tac nhan"); other acceptable VI forms go in target_variants.
- term_type ∈ {term, abbreviation, proper_noun, code_api}. do_not_translate=true for
  framework/library/API/dataset names kept in English.

FOUR BUCKETS:
1. new_terms — controlled terms NOT in MEMORY_PACK. Fields: source_term (singular
   canonical), canonical_target_vi, term_type, do_not_translate, termhood (short reason),
   target_variants[], evidence_block_ids[].
2. updates_to_existing — a MEMORY_PACK term appearing here that gains something: add
   source_variant(s), target_variant(s), evidence_block_ids. A new target_variant is
   allowed ONLY when justified by the English evidence context or by a one-clause reason;
   it MUST carry evidence_block_id and variant_reason; do NOT add a VI variant differing
   only by "các"/"những"; at most 2 new target_variants per term per window. NEVER change
   the existing canonical here.
3. conflicts — when a MEMORY_PACK term's existing canonical VI seems wrong, its surface
   is used in a different sense, or it seems too generic to be a term. Declare, never
   silently fix. Fields: source_term, existing_canonical_target_vi, proposed_target_vi
   (or null), conflict_type ∈ {canonical_target_change, polysemy_suspected,
   bad_existing_target, termhood_suspected, plural_only_difference, uncertain},
   reason (one clause), evidence_block_ids[].
4. seen_existing_terms — MEMORY_PACK terms appearing here that need NO change. Fields:
   source_term, evidence_block_ids[].

RECALL RULE (mandatory): Every controlled source term/concept visible in this window must
be represented exactly once across the four buckets; include all evidence block ids where
it appears. Existing MEMORY_PACK terms are not exempt — if one appears and needs no
change, put it in seen_existing_terms. Never omit a visible term because it "already
exists".

Glossary-only: output only glossary entries; do not output entities, relations, or
motifs. Vietnamese targets must be YOUR OWN proposals or prior notes, never a
reference/gold.

Return JSON:
{ "chapter_id":"...", "window_id":"...", "new_terms":[...], "updates_to_existing":[...],
  "conflicts":[...], "seen_existing_terms":[...] }
```

USER template:
```
MEMORY_PACK
{pack_json}

CHAPTER_ID
{chapter_id}

WINDOW_ID
{window_id}

ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS
{rendered_blocks}
```

*(2 sửa CodeX đã gói: dòng cuối "Glossary-only" KHÔNG còn cấm output VI mới — Builder phải tự đề xuất `canonical_target_vi`; luật `target_variant` bỏ "appears in source-evidence" (vô lý vì source=Anh, target=Việt) → "justified by English evidence or one-clause reason" + thêm field `variant_reason`.)*

### B.3 — Render harness (code)
`pipeline/scripts/builder_v2_render.py --chapter preliminaries --pack-mode proxy_chronological --dry-run --out data/reports/builder_v2_b_render`
Render **≥3 window đại diện**: (a) đầu chương ít-pack, (b) window pack nhiều nhất, (c) window chứa **conflict-fixture Stage A** (`dataset`/`loss`/`activation`). **Nếu chương yêu cầu KHÔNG có fixture đó → report missing + render từ chương khác có, ghi rõ.** In nguyên văn prompt (.txt) + audit (JSON). **0 API** (assert không khởi tạo/gọi LLMClient).

### B.4 — Báo cáo 6 mục bắt buộc
1. Prompt mẫu thật (≥1 .txt). 2. Chính sách context (trong: matched+near_number; ngoài: còn lại + count). 3. Ngân sách token (system/pack/source/output; mỗi window + tổng chương). 4. Cache (prefix ổn định=SYSTEM v8; suffix đổi=pack+window). 5. Điều kiện dừng (halt nếu vượt cap 1500/6000). 6. Cost-quality chiếu (token/window × #window × giá → $/chương cho Stage C).

### B.5 — Acceptance (lệnh chạy được)
- `python pipeline/scripts/builder_v2_render.py --chapter preliminaries --pack-mode proxy_chronological --dry-run --out data/reports/builder_v2_b_render` → ≥3 prompt .txt + audit JSON (8 trường + mode + provenance) + bảng token.
- `python -m pytest pipeline/tests/test_builder_v2_render.py -q`: **assert** (a) 0 LLM call; (b) audit đủ 8 trường; (c) prompt chứa nguyên văn `RECALL RULE` + `termhood_suspected`; (d) pack ≤1500 / prompt ≤6000; (e) **PROVENANCE**: render KHÔNG mở `glossary.md`/`eval_glossary_gold`/reference (`pack_provenance` ∈ {glossary_entries, registry_proxy}); (f) **determinism**: chạy 2 lần ra byte y hệt; (g) `proxy_chronological` không chứa entry có evidence-block sau window (chặn future-leak).

### B.6 — Guards + ghi chú Stage C
0 API/0 DB · prompt v8 verbatim (bump version khi đổi byte) · pack mù-với-gold (chỉ registry-so-far của Builder) · backstop L3 deterministic (single-word ∈ `d2l_term_stoplist.txt` không nhận làm `new_terms` standalone — làm ở Stage C/D, LLM không phải hàng rào duy nhất) · artifact regenerable → gitignore.
**Stage C (ghi nhận, chưa làm):** run THẬT KHÔNG được dùng full frozen v1 registry làm notebook — phải **chronological theo Builder v2** (sổ-tay lớn dần theo thứ tự window). Stage B proxy chỉ để render/đo token.

## 12. Stage B CodeX implementation notes *(CodeX; STOP, không commit)*

Implemented **BUILDER-V2-B render-only** only:

- `pipeline/prepass/builder_v2_render.py`
  - Prompt `d2l_terminology_v8` copied verbatim from §11.
  - Builds filtered `MEMORY_PACK` from `glossary_entries` only.
  - Supports `proxy_chronological` and `proxy_full_registry`.
  - `proxy_chronological` filters registry rows by `evidence_span_ids_json` joined to `blocks.order_index`.
  - Pack includes only `matched_existing_terms` and `near_number_variants`.
  - Stable JSON and deterministic sort; no LLM client.
- `pipeline/scripts/builder_v2_render.py`
  - CLI render harness; requires `--dry-run`.
  - Opens SQLite with `mode=ro`.
  - Renders representative windows and writes prompt `.txt` plus JSON reports.
- `pipeline/tests/test_builder_v2_render.py`
  - Covers prompt contract, audit fields, chronological future-leak guard, full-registry proxy behavior, caps, determinism, and no LLM/gold-source references.

Commands run:

```powershell
cd C:\work\odl-pdf-demo\research\agent-based-translation\THESIS_RUNTIME_TOOL
python -m pytest pipeline/tests/test_builder_v2_render.py -q
python -m py_compile pipeline\prepass\builder_v2_render.py pipeline\scripts\builder_v2_render.py
python pipeline/scripts/builder_v2_render.py --chapter preliminaries --pack-mode proxy_chronological --dry-run --out data/reports/builder_v2_b_render
```

Additional determinism check:

- Rendered twice into two temporary output directories.
- Compared every output file byte-for-byte.
- Result: deterministic `true`.
- Temporary dirs `_tmp_b1/_tmp_b2` removed after path check.

Stage B artifacts:

- `data/reports/builder_v2_b_render/chapter_start_wb_d2l_preliminaries_001.txt`
- `data/reports/builder_v2_b_render/max_pack_wb_d2l_preliminaries_034.txt`
- `data/reports/builder_v2_b_render/conflict_fixture_wb_d2l_preliminaries_011.txt`
- `data/reports/builder_v2_b_render/builder_v2_b_render_report.json`
- `data/reports/builder_v2_b_render/builder_v2_b_pack_audit.json`

Headline Stage B numbers:

| Field | Value |
|---|---:|
| chapter | `d2l_preliminaries` |
| pack_mode | `proxy_chronological` |
| windows | 50 |
| selected prompt files | 3 |
| max_pack_tokens_est | 1500 |
| max_prompt_tokens_est | 4266 |
| total_prompt_tokens_est | 185201 |
| stage_c_upper_bound_tokens_est | 245201 |

Selected windows:

| label | window_id | pack_tokens | prompt_tokens |
|---|---|---:|---:|
| chapter_start | `wb_d2l_preliminaries_001` | 1309 | 3777 |
| max_pack | `wb_d2l_preliminaries_034` | 1500 | 4194 |
| conflict_fixture | `wb_d2l_preliminaries_011` | 1493 | 4235 |

Verification:

- `test_builder_v2_render.py`: **5 passed**.
- `py_compile`: pass.
- Render command: pass.
- Byte determinism: pass.
- DB status: `data/jobs/d2l_p1/memory.sqlite3` not modified.
- No API call path is instantiated; script has no `LLMClient` usage.

CodeX interpretation:

- Stage B satisfies the render-first contract: real prompt, real pack, real token estimates, **0 API / 0 DB write**.
- Prompt remains under `PROMPT_TOKEN_CAP=6000`; pack hits the `PACK_TOKEN_CAP=1500` ceiling on the max-pack sample, so Stage C should keep this cap and monitor dropped items.
- `proxy_chronological` is only a render proxy over frozen v1 registry. Stage C must still use chronological Builder-v2 notebook state, not the full frozen registry.

## 13. §6 — Claude review Stage B *(Claude, 2026-06-29, re-derived ĐỘC LẬP trên cây NFD thật, KHÔNG tin report CodeX)*

**Verdict: PASS Stage B.** Cơ chế sổ-tay + prompt v8 đã chứng minh **trên giấy** đúng hợp đồng: prompt thật, pack thật, token thật, **0 API / 0 DB write**. Mọi số CodeX báo đều tái lập chính xác trên máy mình.

**Đã kiểm chứng (tự chạy lại, không trust report):**
- **Prompt byte-exact:** trích `SYSTEM_PROMPT` trong module so **byte** với block SYSTEM khoá ở §11 → `True`. Prompt = thiết kế Claude, CodeX dùng nguyên byte (đúng giao kèo).
- **Tests:** `pytest test_builder_v2_render.py -q` → **5 passed** (chạy lại trên cây NFD).
- **Render tái lập:** `--chapter preliminaries --pack-mode proxy_chronological --dry-run` → 50 windows; `max_prompt=4266<6000`; `max_pack=1500` (đúng cap); `total=185201`; `stage_c_upper_bound=245201`; 3 prompt mẫu y hệt (001 chapter_start / 034 max_pack / 011 conflict_fixture). **Khớp report tới từng số.**
- **Determinism:** render 2 lần ra 2 thư mục riêng, so **SHA256 từng file** (5/5 file: report 17066B, audit 209460B, 3 .txt) → `det=True` toàn bộ.
- **Frozen DB bất biến:** hash trước = sau = `DA0F687894090D43`; mtime không đổi; `git status data/jobs data/reports` sạch.
- **0 API thật:** module không import/khởi tạo `LLMClient`; estimator `estimate_prompt_tokens` re-derive source → **local, không network**. Test `test_no_llm_or_gold_source_references` chặn `LLMClient`/`glossary.md`/`eval_glossary_gold` trong source.
- **Provenance mù-gold:** `pack_provenance="glossary_entries"`; pack chỉ đọc bảng `glossary_entries`+`blocks`, KHÔNG mở gold. Rendered `.txt` bắt đầu bằng `SYSTEM`, chứa nguyên văn `RECALL RULE (mandatory)`, `conflict_type "termhood_suspected"`, dòng đóng `reference/gold`, và USER có `MEMORY_PACK`+block markers.
- **Chống future-leak (kiểm trên DATA THẬT, không chỉ test giả lập):** window 034 (start order **685**) — re-group độc lập từ DB: cả **27** term included đều có `first_evidence_order < 685` → **0 vi phạm**. Guard `_has_prior_evidence` dùng `<` strict (chặn cả future LẪN same-window).
- **Audit đủ 8 trường** + `pack_source_mode` + `pack_provenance`; `excluded_no_surface_match` = {count, sample[:30]}; `dropped_by_budget` kèm `priority`+`reason`.

**🔴 Phát hiện substantive (xác nhận + làm sắc CodEx's flag) — KHÔNG block Stage B, nhưng BẮT BUỘC sửa trước Stage C:**
Window 034 chạm cap 1500 → **drop 15 term**. Soi danh sách: drop theo **alphabet-tail** (greedy sort `(priority, source_term)` rồi cắt phần cuối), nên rớt đúng các term **mang-tính-nhất-quán**: `scalar`, `shape`, `vector`, `column vectors`, `dot products`, `components`… chỉ vì xếp cuối bảng chữ — KHÔNG phải vì kém quan trọng. Đây chính là **precision-at-inject** mình đã cờ (refinement #3): pack đang chọn-bỏ bằng alphabet thay vì theo **mức nhất-quán-cần-giữ**. Bản thân Stage B render-để-đo đã làm đúng việc của nó — **phơi bày lỗi này trên giấy TRƯỚC khi tiêu API**. Sửa ở Stage C: ưu tiên pack theo `occurrences_count` / term-có-conflict-lịch-sử / multi-word-termhood (KHÔNG alphabet); và đo lại `dropped_by_budget` sau khi đổi thứ tự ưu tiên.

**Ghi nhận cho Stage C (chốt lại):**
1. Pilot THẬT **không** được dùng full frozen v1 registry làm sổ-tay — phải chronological theo Builder v2 (sổ lớn dần). `proxy_chronological` Stage B chỉ để render/đo token.
2. Thay greedy-alphabet bằng **priority-budget** (consistency-bearing trước); re-render đối chiếu `dropped_by_budget`.
3. Cap 1500/6000 giữ nguyên; theo dõi `dropped_by_budget` mỗi window nặng.
4. Frozen DB `mode=ro`; pilot ghi artifact JSON, KHÔNG ghi DB; gold chỉ để CHẤM (DEV, không headline).

**Guards review:** prompt v8 verbatim (bump version khi đổi byte) · pack mù-gold · backstop L3 deterministic (single-word ∈ stoplist không nhận `new_terms` standalone — làm Stage C/D, LLM không phải hàng rào duy nhất) · artifact `data/reports/builder_v2_*/` regenerable → đã gitignore (`git check-ignore` xác nhận), KHÔNG commit.

## 14. Stage C1 — Consolidation engine online + pack-slim + offline sim (0 API) *(Claude spec; CodeX implement §5)*

**Mục tiêu:** dựng & CHỨNG MINH "máy dọn rác" (L3 consolidation) với **0 API** TRƯỚC khi C2 tiêu tiền LLM. Tách Stage C: **C1 = cơ chế (0 API, deterministic, test được)** → nghiệm thu → **C2 = pilot gọi LLM thật (cost-gate)**. Lý do tách (user+CodeX đồng ý): lọc/gộp phải chạy **online sau MỖI window** (cuốn sổ-tay dùng cho window kế phải sạch ở mọi bước); lọc-cuối-chương chỉ làm đẹp report, vô dụng cho lúc chạy.

### 14.0 Verdict góp ý CodeX *(Claude: nhận 3 điểm chính + sub-points, THÊM 3 refinement)*
**Nhận (đúng, đưa vào spec):** QĐ-1 → "**first valid canonical wins provisionally**" (provisional tới freeze cuối phase; window sau KHÔNG ghi đè, chỉ conflict-log) · QĐ-2 → **source-side luôn gộp; target-side KHÔNG ghi đè canonical; phân loại divergence** · QĐ-3 → `source_variants` là **record có cấu trúc** (không phải list string) · offline simulation trên data thật · stoplist predicate hợp thành (không phải "in list thì bỏ") · decision-log mọi quyết định · pack flag `conflict_pending`.

**Claude refinement (advisor, BẮT BUỘC trong spec):**
- **R1 (QUAN TRỌNG NHẤT — gỡ mâu thuẫn): tách NOTEBOOK (giàu) vs PACK (gầy).** QĐ-3 muốn `source_variants` mang `evidence_block_ids`/`occurrence`/`first_seen_window`; nhưng ở §13 ta đã chốt **bỏ `evidence_block_ids` khỏi pack** để tiết kiệm token. KHÔNG mâu thuẫn nếu để đúng chỗ: **record giàu sống trong NOTEBOOK/artifact (để audit/consolidate); PACK chỉ là hình chiếu GẦY** (surface trần + canonical + ≤2 variant string + status). Nếu CodeX nhét record giàu vào pack → **phình lại**. Phải tách rõ trong spec.
- **R2 (trung thực phạm vi offline sim): KHÔNG có bảng `glossary_candidates` thô** (Claude đã probe DB: chỉ `glossary_entries` n=1608 đã qua casefold-merge của v7). Nên offline sim **replay `glossary_entries`** → đo **hiệu lực TĂNG THÊM của `concept_key`+stoplist so với casefold** (1608→~1486 + số rác bị reject), KHÔNG phải "raw candidates → sạch". Và vì v7 chạy registry-OFF nên output chỉ có ứng viên thô → sim **chỉ tập** create/merge/stoplist/number, **KHÔNG** chạm update/conflict/seen (mấy rổ đó cần LLM thật ở C2). Ghi rõ giới hạn này, không over-claim C1 = C2 dry-run.
- **R3 (stoplist phải DETERMINISTIC): "strong termhood reason" = predicate đo được**, không phải code "đọc cảm tính" free-text của LLM. Cụ thể: termhood mạnh = (≥2 evidence block) **HOẶC** block_type ∈ {heading, definition, math} **HOẶC** concept_key đã có trong notebook.

### 14.1 Data model — NOTEBOOK (artifact JSON, GIÀU) *(R1)*
Mỗi entry: `concept_key` · `canonical_target_vi` (provisional) · `term_type` · `do_not_translate` · `status` ∈ {`ok`, `conflict_pending`} · `occurrences_total` · `first_seen_window` · `source_variants[]` (mỗi: `surface`, `match_type`∈{exact,number_variant}, `evidence_block_ids[]`, `occurrence_count`, `first_seen_window`) · `target_variants[]` (mỗi: `text`, `evidence_block_id`, `variant_reason`) · `conflict_ledger[]` (mỗi: `type`, `proposed_target`, `reason`, `window`, `evidence_block_ids[]`) · `decision_log[]` (xem 14.2).

### 14.2 Consolidation ONLINE — mỗi window, NGAY sau khi LLM trả *(L3 = quyền cuối)*
Vòng: `[notebook sạch tới i-1] → code dựng PACK (14.3) → LLM đề xuất 4 rổ → CODE gộp/lọc vào notebook NGAY → [notebook sạch tới i]`. Xử lý từng rổ:
- **`new_terms`:** (a) `concept_key`. (b) **stoplist predicate (R3, deterministic):** reject standalone iff `single-token` ∧ `∈stoplist` ∧ `∉allowlist` ∧ `occurrence/evidence<2` ∧ `không có block_type∈{heading,definition,math}` ∧ `concept_key chưa trong notebook` → log `rejected_stoplist`. (c) nếu `concept_key` đã có → route sang update (log `merged_by_concept_key`). (d) còn lại → tạo entry, `canonical = LLM canonical_target_vi` nếu valid (non-empty), `first_seen_window=i` (log `created`).
- **`updates_to_existing`:** union `source_variants` (thêm record surface) + `target_variants` (≤2/window, bỏ biến thể chỉ khác `các/những`, **bắt buộc** evidence+reason), cộng occurrence/evidence. **KHÔNG đổi canonical.** (log `updated_source_variant`/`updated_target_variant`).
- **`conflicts`:** append `conflict_ledger`; nếu type ∈ {`canonical_target_change`,`polysemy_suspected`,`bad_existing_target`} → set `status=conflict_pending`. **KHÔNG mutate canonical.** (log `conflict_logged`).
- **`seen_existing_terms`:** chỉ cộng occurrence/evidence (log `seen_existing`).

**Phân loại divergence target (code-side, QĐ-2 — khi 1 update/new đề xuất target ≠ canonical cùng `concept_key`):**
1. normalize `các/những` → bằng nhau ⇒ `plural_only_difference` (bỏ/hạ nhẹ, **không** conflict lớn).
2. khác nhẹ kiểu đồng nghĩa/văn phong ⇒ `synonym_or_style_variant` ⇒ thêm vào `target_variants` (**phải có evidence**), `status` giữ `ok`.
3. còn lại (đổi nghĩa/sai) ⇒ `polysemy_suspected`/`bad_existing_target` ⇒ `conflict_ledger` + `status=conflict_pending`; **KHÔNG** thêm vào accepted `target_variants`. *(Ví dụ nguy hiểm `loss` "hàm mất mát" vs "giá trị mất mát": giữ canonical, ghi conflict, KHÔNG trộn mù; hướng sửa = prompt sau ưu tiên term cụ thể `loss function`/`loss value`.)*

`decision_log` enum (bắt buộc mỗi term mỗi window): `created` · `merged_by_concept_key` · `updated_source_variant` · `updated_target_variant` · `rejected_stoplist` · `conflict_logged` · `seen_existing`.

### 14.3 Pack-slim — sửa `builder_v2_render.py` *(R1 + §13 carry-over)*
- **Pack item = hình chiếu GẦY:** `source_term`, `canonical_target_vi`, `allowed_variants[:2]` (string), `term_type`, `do_not_translate`; **NẾU** `status=conflict_pending` → thêm `"status":"conflict_pending"`. **BỎ `evidence_block_ids` khỏi pack** (record giàu ở notebook).
- **Nén JSON pack trong prompt** (bỏ indent) + **sửa lỗi §13**: ước tính token đo ĐÚNG bytes được nhét (kết thúc lệch compact-vs-indent).
- **Cắt khi quá ngân sách theo ƯU TIÊN, không theo bảng chữ:** sort key = (`conflict_pending` trước → `occurrences_total` desc → multiword trước → tên). Giữ term mang-tính-nhất-quán cao nhất.
- Cap giữ 1500/6000; **đo lại `dropped_by_budget`** sau slim+lean; chỉ nâng `1500→2500` nếu vẫn rớt term THẬT ở window đặc (đúng ưu tiên user: thiếu-term > token).

### 14.4 Offline simulation — script mới, 0 API *(CodeX + R2)*
- Đọc `glossary_entries` (frozen DB `mode=ro`). Coi mỗi entry như 1 đề xuất `new_terms` **"đến" ở window theo `first_evidence_order`** (chronological; entry nhiều evidence → lấy block sớm nhất).
- Chạy consolidation 14.2 **theo thứ tự window**.
- Report: entry trước/sau · `rejected_stoplist` (count+sample) · `merged_by_concept_key` (count) · conflicts (count+types) · **occurrence bảo toàn** (before==after) · evidence bảo toàn.
- **Caveat ghi rõ (R2):** chỉ exercise create/merge/stoplist/number; update/conflict/seen cần LLM thật (C2). Đây KHÔNG phải C2 dry-run.

### 14.5 Tests
Unit từng luật (created/merged/rejected/update-variant/conflict/seen) · guard polysemy (`loss` value vs function → `conflict_pending`, KHÔNG trộn target) · `plural_only` normalize · stoplist predicate (`set` trong heading → giữ; `set` trần → reject) · decision-log đầy đủ · determinism (2 lần giống byte). Offline sim như integration: assert occurrence bảo toàn + entry giảm + đủ trường report.

### 14.6 Guards + Acceptance
**Guards:** 0 API / 0 DB write (frozen hash `DA0F687894090D43` ro) · KHÔNG đụng production `glossary_entries` (chỉ artifact JSON) · mù-gold · prompt v8 KHÔNG đổi (C1 không gọi LLM) · decision-log = audit đầy đủ · artifact gitignore.
**Acceptance (lệnh chạy được):** `python pipeline/scripts/builder_v2_consolidate_sim.py --doc-id d2l --out data/reports/builder_v2_c1_sim` → report 0-API + `pytest pipeline/tests/test_builder_v2_consolidate.py -q` pass + render lại pack slim (`builder_v2_render.py`) cho thấy `dropped_by_budget` giảm + token/term giảm vs §13.
**C2 (ghi nhận, CHƯA làm):** pilot 1 chương gọi LLM thật qua cơ chế C1 online → artifact registry v2 (KHÔNG ghi DB) → đo entry vs v1 / recall-vs-gold (DEV) / conflict-rate / token-window / occurrence-bảo-toàn; cost-gate $/chương user duyệt TRƯỚC.

## 15. §5 — CodeX implementation notes Stage C1 *(CodeX, 2026-06-29; STOP, no commit/push)*

**Scope implemented:** C1 only. No API, no LLM pilot, no migration, no writes to production `glossary_entries`.

Files changed:
- `pipeline/prepass/builder_v2_consolidate.py` — new online consolidation engine with rich notebook model, four-bucket handlers, deterministic stoplist predicate, target divergence classification, conflict ledger, and decision log.
- `pipeline/prepass/builder_v2_render.py` — pack-slim projection: compact JSON in prompt, no `evidence_block_ids` in pack payload, priority budget sort (`conflict_pending` → occurrences desc → multiword → name), conflict status passthrough when present.
- `pipeline/scripts/builder_v2_consolidate_sim.py` — new read-only `glossary_entries` replay simulation.
- `pipeline/tests/test_builder_v2_consolidate.py` — unit/integration coverage for created/merged/rejected/update/conflict/seen, `loss` polysemy, plural-only, stoplist, determinism, offline sim conservation.
- `pipeline/tests/test_builder_v2_render.py` — updated Stage B/C1 pack-slim expectations.

Implementation details:
- Notebook is rich (`source_variants` carry evidence/occurrence/window); pack remains lean (`source_term`, `canonical_target_vi`, `allowed_variants[:2]`, `term_type`, `do_not_translate`, optional `status`). This preserves R1.
- Offline sim replays only `glossary_entries` in chronological first-evidence order. It does not claim to simulate real C2 four-bucket LLM output; report records this R2 caveat.
- Stoplist uses the hard R3 predicate only: single-token stoplist terms are rejected only when not allowlisted, not already in notebook, evidence block count <2, and no evidence block type is in `{heading, definition, math, math_block}`.
- Occurrence conservation is checked against `occurrence_input_effective`. Historical rows with `occurrences_count<=0` use `max(1, evidence_count)` in the replay; raw DB total is reported separately as `occurrence_input_raw_db`.

Commands run:

```powershell
cd C:\work\odl-pdf-demo\research\agent-based-translation\THESIS_RUNTIME_TOOL
python -m py_compile pipeline\prepass\builder_v2_consolidate.py pipeline\prepass\builder_v2_render.py pipeline\scripts\builder_v2_consolidate_sim.py pipeline\scripts\builder_v2_render.py
python -m pytest pipeline\tests\test_builder_v2_consolidate.py pipeline\tests\test_builder_v2_render.py -q --basetemp D:\temp\pytest-builder-v2-c1
python pipeline\scripts\builder_v2_consolidate_sim.py --doc-id d2l --out data\reports\builder_v2_c1_sim
python pipeline\scripts\builder_v2_render.py --chapter preliminaries --pack-mode proxy_chronological --dry-run --out data\reports\builder_v2_c1_render
```

Verification:
- `py_compile`: pass.
- Targeted tests: **12 passed**.
- DB hash before/after acceptance: `DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8`; `git status -- data/jobs/d2l_p1/memory.sqlite3` empty.
- No API call path used; scripts use local token estimator / SQLite read-only only.

Stage C1 simulation artifact:
- Output dir: `data/reports/builder_v2_c1_sim/`
- `raw_entries`: **1608**
- `notebook_entries`: **1484**
- `after_notebook_plus_rejected`: **1486**
- `delta_notebook`: **-124**
- `rejected_stoplist`: **2** (`set`, `problem` under the hard predicate)
- `merged_by_concept_key`: **122**
- `conflicts`: **8** (`plural_only_difference`: 5, `polysemy_suspected`: 3)
- `occurrence_conserved`: **true** (`effective input=13371`, notebook=13127, rejected=244)
- `evidence_conserved`: **true** (`input=4371`, notebook=4369, rejected=2)

Pack-slim re-render on `preliminaries`:

| Metric | Stage B (§13) | Stage C1 slim |
|---|---:|---:|
| windows | 50 | 50 |
| max_pack_tokens_est | 1500 | 1495 |
| max_prompt_tokens_est | 4266 | 3579 |
| total_prompt_tokens_est | 185201 | 141317 |
| dropped_by_budget total | 133 | 16 |

Interpretation:
- C1 fixes the Stage B alphabet-tail failure mode substantially: dropped pack items in preliminaries fell from **133 → 16**, and total prompt estimate fell by **43,884 tokens** for the chapter.
- Max pack still reaches near cap (**1495/1500**), so C2 should keep `dropped_by_budget` as a hard audit field and not raise budget blindly.
- C1 simulation is intentionally conservative: it validates consolidation mechanics over historical `glossary_entries`; it is not evidence that the real LLM will fill `updates_to_existing/conflicts/seen_existing_terms` correctly in C2.

## 16. §6 — Claude review Stage C1 *(Claude, 2026-06-29, re-derived ĐỘC LẬP trên cây NFD, KHÔNG tin report CodeX)*

**Verdict: PASS Stage C1.** Máy-dọn-rác (L3 consolidation online) + pack-slim đã dựng đúng spec §14, deterministic, **0 API / 0 DB write**. Mọi số CodeX báo đều tái lập, và cú "occurrence_conserved" mình soi kỹ → **trung thực, không phải định nghĩa lại cho xanh**.

**Đã kiểm chứng (tự chạy lại + re-derive ĐỘC LẬP từ DB):**
- **Tests 12/12** (7 `test_builder_v2_consolidate` + 5 `test_builder_v2_render`) — và **không nông**: test polysemy `loss`/`losses` → `conflict_pending` + `target_variants` RỖNG (không trộn mù), stoplist `set` prose→reject / heading→giữ (R3), canonical bất biến khi update, occurrence conserved, determinism, integration sim trên DB thật.
- **Sim tái lập:** 1608 → **1484** notebook, 2 rejected, **122 merged_by_concept_key**, 8 conflicts (5 plural_only + 3 polysemy).
- **Re-derive ĐỘC LẬP từ DB (không qua sim):** `concept_key` gom 1608 → 1486 nhóm = **−122** (khớp Stage A chính xác); 1608 − 122 − 2 = **1484** ✓.
- **🔬 Soi "occurrence conserved" (nghi fudge):** raw v7 = 13252; có **71 entry `occurrences_count=0` NHƯNG có evidence block** (anomaly v7, 4.4%). Engine bump `0→max(1,evidence)` → effective = **13371** (+119, mình tự tính khớp). Conservation check đúng dạng `effective_input == notebook + rejected` = `13371 = 13127 + 244` ✓. Đây là cách hiểu ĐÚNG (entry có evidence ⇒ đã xuất hiện ≥1 lần, count=0 là lỗi dữ liệu), và report **giữ cả raw lẫn effective + note minh bạch** → KHÔNG che giấu mất mát. Evidence conserved cũng đúng.
- **Pack-slim (sửa lỗi §13):** total prompt **185201 → 141317** (−43.884), `dropped_by_budget` **133 → 16**. **R1 đạt:** pack KHÔNG còn `evidence_block_ids`, JSON **nén** (item không xuống dòng) → record giàu chỉ ở notebook. **Cắt-theo-ưu-tiên thật** (`_candidate_priority_sort`): `conflict_pending → occurrences desc → multiword → exact`, bảng-chữ tụt xuống tiebreak cuối — đúng §14.3, không phải chỉ nhờ slim mà giảm drop.
- **Determinism:** notebook JSON byte y hệt 2 lần chạy. **Frozen hash `DA0F687894090D43` trước=sau**, `data/jobs`+`data/reports` git sạch (0 DB write), artifact gitignore.

**🟡 Trung thực về phạm vi (BẮT BUỘC nhớ khi đọc "1608→1484"):**
- Giảm bloat ở C1 **gần như TOÀN BỘ là number-merge (122)**, KHÔNG phải lọc-từ-phổ-thông (chỉ **2**: `set`, `problem`). Vì replay entry đã giàu occurrence/evidence nên hầu hết qua được ngưỡng <2-evidence, và guard "concept_key đã có trong notebook" khiến bản trùng bị *merge* chứ không *reject*. → **Giá trị thật của stoplist CHƯA được chứng minh tới C2** (đúng caveat R2). Đừng bán "1608→1484 = đã dọn rác phổ thông"; nó là dọn **số-ít/số-nhiều**.
- Stoplist code = đúng 12 từ trong new-term-restraint của prompt (input/output/value…), KHÔNG gồm feature/function/model/layer → backstop hẹp; phần rộng dựa vào PROMPT ở C2.
- `_looks_polysemous` là heuristic hẹp (hardcode "hàm"/"giá trị" + disjoint-token). Ổn cho cơ chế **chỉ-gắn-cờ-để-review** (không tự sửa phá hoại), nhưng sẽ sót polysemy tinh vi → ghi nhận cho C2/Phase D.

**Ghi nhận cho C2 (gọi LLM thật):** dùng notebook **chronological v2** (KHÔNG bê v1); occurrence/priority lấy từ notebook v2 đang lớn dần (không phải v1); giữ `dropped_by_budget` làm audit cứng, chỉ nâng cap 1500→2500 nếu rớt term THẬT; cost-gate $/chương duyệt TRƯỚC; đo entry vs v1 + recall-vs-gold (DEV) + conflict-rate + occurrence-bảo-toàn. **C2 mới là nơi đo được stoplist + 4-rổ thật.**

## 17. Stage C2 — Pilot 1 chương gọi LLM thật (online loop) *(Claude spec; CodeX implement §5)*

**Mục tiêu:** chạy Builder v2 THẬT trên 1 chương (`preliminaries`, 50 window) qua vòng online `pack→LLM(v8)→4 rổ→engine C1→window kế`, ghi **artifact JSON (KHÔNG ghi DB)**, rồi ĐO v2 vs v1 (DEV). Đây là nơi đầu tiên 4 rổ + stoplist + 3 QĐ được kiểm trên hành vi LLM thật. **Có API → cost-gate user duyệt TRƯỚC.**

### 17.0 Carried-locked (từ A/B/C1, KHÔNG mở lại)
Prompt `d2l_terminology_v8` verbatim · engine C1 = quyền cuối (`apply_builder_output`) · pack-slim + cắt-ưu-tiên · 3 QĐ (first-valid-canonical-provisional / source-merge-không-overwrite-target / source_variants giàu) · mù-gold · frozen DB `mode=ro` · artifact JSON only.

### 17.1 Driver online (CODE MỚI — `pipeline/scripts/builder_v2_pilot.py`)
Sổ-tay khởi tạo **RỖNG**, lớn dần theo thứ tự window:
```
notebook = Notebook()
for window in build_d2l_prepass_windows(chapter):   # đã có
    pack = build_pack_from_notebook(notebook, window)        # 17.2 (mới)
    messages = build_builder_v2_messages(pack, ...window)    # đã có (prompt v8)
    resp = llm_call_cached(messages, model, params)          # 17.3/17.4 (API + cache)
    parsed = parse_4buckets(resp)                            # validate JSON contract
    apply_builder_output(notebook, parsed, window_id, block_types)  # engine C1
emit: notebook.json + decision_log.json + cost_log.json + per_window_audit.json
```
**Tự nhiên chống future-leak:** pack dựng TRƯỚC khi cập nhật notebook → sổ chỉ chứa window trước → KHÔNG cần proxy_chronological nữa (đúng "chronological thật").
**JSON contract guard + DEGRADED (sửa theo CodeX #6):** mỗi resp phải parse được + đủ 4 khoá; lỗi parse → log `parse_failure`, **re-ask 1 lần** (`bypass_cache=True` đã có trong client) trước khi bỏ qua; vẫn lỗi → bỏ qua window (không crash, KHÔNG bịa rỗng). **Đếm `parse_failure_count`; nếu >0 → run `status=degraded`.** Acceptance: run `degraded` **CHỈ để debug, KHÔNG được rút kết luận chất lượng** (skip 1 window = sổ thiếu term cho các window sau).

### 17.2 Pack đọc từ notebook v2 SỐNG (sửa, không proxy v1)
Adapter biến `NotebookEntry` → cấu trúc `build_memory_pack` đang dùng (source_term, canonical_target_vi, allowed_variants≤2, occurrences_total, status). **occurrence/priority = số v2 THẬT** (không phải v1).
**Match MỌI surface (sửa theo CodeX #5):** `build_memory_pack` cũ chỉ quét `entry.source_term` + number-variant SINH RA từ canonical → sẽ MISS surface thật đã lưu. C2 phải quét **toàn bộ `entry.source_variants[].surface`** (vì notebook gộp `feature/features` về 1 entry — nếu chỉ đưa canonical thì pack có thể bỏ sót `features` trong window sau). Tức adapter expose hết surface đã biết; pack-match dùng tập đó, không chỉ canonical.
Tái dùng nguyên: cắt-ưu-tiên (`conflict_pending→occ desc→multiword→exact`), slim (bỏ evidence, nén JSON), cap 1500/6000.

### 17.3 Model + tham số *(CHỐT sau verify config v7 + góp ý CodeX #3)*
- **DÙNG NGUYÊN config v7 verbatim** (`pipeline/configs/llm_prepass.yaml` — Claude đã verify): `model=gpt-5.4-mini`, `temperature=1.0`, `seed=20260612`, `reasoning_effort=none`, `verbosity=low`, `max_output_tokens=6144`, `response_format={"type":"json_object"}`. **KHÔNG đổi temp/max_output** (bỏ đề xuất temp=0 cũ).
- **Lý do (sửa theo CodeX #3 + phát hiện seed):** giữ nguyên decoding ⇒ decoding KHÔNG còn là biến gây nhiễu khi so v1↔v2; tái lập KHÔNG cần temp=0 vì (a) v7 đã có `seed` cố định, (b) **cache (17.4) mới là cơ chế tái lập thật** (lần 2 = cache-hit = byte y hệt, bất kể temp).
- **Trung thực:** C2 vẫn là so **cấp-hệ-thống** (prompt v8 + cơ chế online-notebook GỘP CHUNG) vs v1 — decoding hết là confound, nhưng prompt và cơ chế đổi cùng lúc ⇒ KHÔNG quy được nhân-quả cho riêng prompt. Đủ cho pilot (hỏi "thiết kế v2 tổng thể có tốt hơn không"), không claim ablation đơn-yếu-tố.

### 17.4 Cache + cost-gate + halt *(sửa theo CodeX #1, #2, #4)*
- **Cache DB RIÊNG (CodeX #1 — guard violation đã sửa):** TUYỆT ĐỐI không ghi `llm_call_cache` vào frozen `data/jobs/d2l_p1/memory.sqlite3` (đang `mode=ro`, hash bất biến). Trỏ `LLMClient` cache sang artifact riêng **`data/reports/builder_v2_c2_pilot/llm_cache.sqlite3`** (cache-write hợp lệ vì là artifact, không phải production DB; đã gitignore).
- **Cache key — guard (CodeX #2).** ⚠️ **ĐÍNH CHÍNH (xem §19):** Claude bản trước nói "thiếu cả `model`" là **SAI** (đọc thiếu 1 dòng do grep cắt context). Sự thật `llm_client.py:88`: key = `hash(model, messages, temperature, seed, reasoning_effort, response_format)` — CÓ `model`, CHỈ thiếu `max_output_tokens` + `verbosity` (đúng như CodeX nêu ban đầu). **Cách xử lý C2 (blast-radius = 0):** dùng **cache DB MỚI TINH + 1 config CỐ ĐỊNH** cho cả run ⇒ mọi key do chính C2 ghi dưới đúng 1 config ⇒ stale-hit BẤT KHẢ, các field thiếu thành vô hại. Ghi **full config vào artifact + `tag`** để kiểm. *(Sửa cache-key chung trong `LLMClient` là việc RIÊNG: đổi key ⇒ vô hiệu TOÀN BỘ cache production hiện có → KHÔNG gộp vào C2; ghi follow-up.)*
- **Cost-gate 2 bước:** (a) `--estimate-only` in token+$ **upper-bound bảo thủ** rồi THOÁT (0 API); (b) chỉ chạy khi `--confirm-usd <ceiling>`; ước tính > ceiling → halt.
- **Ước tính BẢO THỦ (CodeX #4 — KHÔNG dùng notebook rỗng):** lấy nền từ C1 đo thật, KHÔNG chạy online giả với sổ rỗng (pack rỗng → rẻ giả). Pricing THẬT (config v7): input $0.25/1M, cached_input $0.025/1M, output $2.00/1M.
  - Thực tế: prompt **141.317** × $0.25/1M = **$0.035** + output ~60.000 × $2/1M = **$0.12** → **≈$0.155/chương**.
  - Upper-bound (output chạm cap 6144×50=307k): $0.035 + $0.61 = **≈$0.65/chương**.
  - **1 chương rất rẻ; gate là kỷ luật.** (Daily cap config = 2.4M tok ≫ ~201k/chương.)
- **Halt:** per-window assert pack≤1500/prompt≤6000 (đã có); running token total log; vượt ceiling giữa chừng → dừng + audit.

### 17.5 Metrics (deliverable — so v2 vs v1, **scope khớp**)
1. **Entry count:** v2 (chương này) vs **v1 ĐÚNG chương này** (scope-match, KHÔNG so với 1608 toàn sách — scoring-scope-equals-production-scope).
2. **Recall-vs-gold (DEV):** tái dùng `builder_gold.score_builder_vs_gold`; bao nhiêu gold-term của chương này v2 bắt được, so v1. **Gold CHỈ để chấm sau, KHÔNG bơm vào builder.**
3. **Stoplist value THẬT:** đếm bao nhiêu từ-phổ-thông LLM đề xuất bị code reject (vá lỗ R2 — C1 chỉ thấy 2).
4. **Số-ít/số-nhiều:** v2 còn tách đôi không (kỳ vọng ~0 nhờ concept_key).
5. **Conflict** (count+types) · **occurrence bảo toàn** trong run v2 · **token/window thật vs ước** · phân bố `decision_log`.
6. **Chống tuning-on-test:** chương pilot = **DEV**, recall-vs-gold KHÔNG phải headline; KHÔNG chỉnh prompt/stoplist theo gold chương này (dont-tune-intervention-on-test-baseline).

### 17.6 Guards
0 DB write (artifact JSON only) · frozen DB `mode=ro` (chỉ đọc source blocks) · mù-gold (gold chỉ ở bước chấm, sau) · prompt v8 verbatim · key env-first→`OPENAI-KEY-*.txt`, **KHÔNG log** · per-window audit + decision/cost log đầy đủ · artifact `data/reports/builder_v2_c2_*` gitignore.

### 17.7 Acceptance (lệnh chạy được)
- `python pipeline/scripts/builder_v2_pilot.py --chapter preliminaries --estimate-only` → in token+$ ước tính, **0 API**, thoát.
- `python pipeline/scripts/builder_v2_pilot.py --chapter preliminaries --confirm-usd <ceiling> --out data/reports/builder_v2_c2_pilot` → chạy thật, artifact notebook+decision+cost+audit; frozen hash bất biến; metrics §17.5 in ra.
- `pytest pipeline/tests/test_builder_v2_pilot.py -q`: mock LLM (KHÔNG gọi API thật trong test) → assert (a) loop online dựng pack từ notebook sống + future-leak=0; (b) cache-hit lần 2 = 0 API; (c) parse_failure không crash; (d) cost-gate chặn khi >ceiling; (e) metrics scope-match v1 đúng chương; (f) 0 DB write.

### 17.8 Vòng siết theo góp ý CodeX *(Claude: NHẬN cả 6, đã VERIFY trên file thật)*
| # | CodeX nêu | Verdict + xử lý |
|---|---|---|
| 1 | Cache không được ghi frozen DB | **NHẬN (guard violation).** Cache → DB artifact riêng `data/reports/builder_v2_c2_pilot/llm_cache.sqlite3`, frozen `memory.sqlite3` giữ ro. → 17.4 |
| 2 | Cache key thiếu `max_output_tokens`/`verbosity` | **NHẬN. ⚠️ ĐÍNH CHÍNH (xem §19): Claude trước nói "thiếu cả `model`" là SAI** — `llm_client.py:88` CÓ `"model": self.config.model`; key = `hash(model,messages,temp,seed,reasoning,response_format)`, CHỈ thiếu `max_output_tokens`+`verbosity` (đúng như CodeX nêu ban đầu). Xử lý C2: cache MỚI + 1 config CỐ ĐỊNH ⇒ stale-hit bất khả, field thiếu vô hại; sửa key chung = follow-up riêng. → 17.4 |
| 3 | "Same model v7" chưa đủ nếu đổi decoding | **NHẬN, sửa MẠNH hơn:** verify `llm_prepass.yaml` = gpt-5.4-mini/temp1.0/**seed20260612**/reasoning-none/verbosity-low/max6144. → **dùng NGUYÊN config v7** (bỏ temp=0), decoding hết là confound; tái lập = seed + CACHE. C2 = so cấp-hệ-thống, không claim ablation. → 17.3 |
| 4 | Estimate-only không được dùng notebook rỗng | **NHẬN.** Estimate = upper-bound bảo thủ từ C1 (141k prompt + output cap) × pricing thật → $0.155 thực / $0.65 trần. → 17.4 |
| 5 | Adapter phải match mọi `source_variants[].surface` | **NHẬN.** Pack quét toàn bộ surface đã lưu, không chỉ canonical+number-variant. → 17.2 |
| 6 | Parse failure không được "skip coi như xong" | **NHẬN.** re-ask 1 lần → `parse_failure_count` → `status=degraded` nếu >0 → run degraded chỉ debug, không rút kết luận chất lượng. → 17.1 |

**Acceptance bổ sung:** test cache-hit chạy trên **cache DB riêng** (xác nhận KHÔNG đụng frozen); test `--estimate-only` ra số > 0 dù notebook rỗng (dùng upper-bound); test `degraded` set khi mock 1 parse-fail.

**Quy trình:** CodeX điền §5 implementation + chạy `--estimate-only` ghi số → **STOP, KHÔNG gọi API thật, KHÔNG commit**. Claude review §6 + **trình $ cho user duyệt** → user OK mới chạy `--confirm-usd`.

## 18. §5 — CodeX implementation notes Stage C2 estimate-only *(CodeX, 2026-06-29; STOP, no commit/push)*

**Scope implemented:** C2 driver + estimate-only gate only. Real API pilot was **not** run.

Files changed:
- `pipeline/scripts/builder_v2_pilot.py`
  - New online C2 driver with empty live `Notebook()` growing window-by-window.
  - Builds pack **before** applying the current window output, preventing future leak by construction.
  - Builds pack from live v2 notebook and scans **all `source_variants[].surface`** plus near-number variants.
  - Uses prompt v8 via `build_builder_v2_messages`; prompt text unchanged.
  - Uses separate artifact cache DB path `data/reports/builder_v2_c2_pilot/llm_cache.sqlite3` for real runs; frozen DB is opened/read only for chapter windows.
  - Implements parse guard: required four buckets, re-ask once with `bypass_cache=True`, then marks run `degraded` and skips that window if still invalid.
  - Implements cost gate: `--estimate-only` exits before LLM client/key setup; real run requires `--confirm-usd`.
- `pipeline/tests/test_builder_v2_pilot.py`
  - Mock-only tests for live-notebook pack/future-leak, separate cache + second-run cache hits, parse-failure degraded status, cost gate, and C1-based estimate-only.

Important verification note:
- Current `pipeline/agents/llm_client.py` **does include `model` in the cache key**. It still omits `max_output_tokens` and `verbosity`. C2 does not change production cache-key behavior; it uses a fresh cache DB plus fixed v7 config, so stale-hit risk is out of scope for C2.

Acceptance run:
- `python -m pytest pipeline\tests\test_builder_v2_pilot.py -q --basetemp D:\temp\pytest-builder-v2-c2` → **5 passed**
- `python -m pytest pipeline\tests\test_builder_v2_consolidate.py pipeline\tests\test_builder_v2_render.py pipeline\tests\test_builder_v2_pilot.py -q --basetemp D:\temp\pytest-builder-v2-all` → **17 passed**
- `python pipeline\scripts\builder_v2_pilot.py --chapter preliminaries --estimate-only` → **0 API**, output:
  - calls: **50**
  - estimated_prompt_tokens: **141317**
  - estimated_output_tokens_nominal: **60000**
  - estimated_output_tokens_cap: **307200**
  - estimated_total_tokens_nominal: **201317**
  - estimated_total_tokens_cap: **448517**
  - estimated_cost_usd_nominal: **0.15532925**
  - estimated_cost_usd_cap: **0.64972925**
  - model config: `gpt-5.4-mini`, temperature `1.0`, seed `20260612`, reasoning `none`, verbosity `low`, max_output_tokens `6144`

Frozen DB:
- `data/jobs/d2l_p1/memory.sqlite3` SHA256 remains `DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8`.

**STOP condition honored:** no `--confirm-usd`, no real API, no commit, no push.

## 19. §6 — Claude review Stage C2 estimate-only *(Claude, 2026-06-29, re-derived ĐỘC LẬP)*

**Verdict: PASS C2 estimate-only — SẴN SÀNG chạy thật, CHỜ user duyệt $.** Driver online đúng spec §17 đã siết; mọi số tái lập; 0 API; 0 side-effect.

**⚠️ Tự đính chính trước (CodeX bắt đúng):** ở §17.8/§17.4 mình từng khẳng định cache key "thiếu cả `model`" — **SAI**. Đọc lại `llm_client.py:88` có `"model": self.config.model`. Lỗi do mình kết luận từ một lần `grep` mà cửa sổ context cắt mất đúng dòng `model` (1 dòng trên `messages`), không đọc cả khối. Sự thật: key = `hash(model, messages, temperature, seed, reasoning_effort, response_format)`, chỉ thiếu `max_output_tokens`+`verbosity` — **đúng như CodeX nêu ban đầu**. Đã sửa §17.8 + §17.4. (Bài học: claim "thiếu field X" phải đọc nguyên khối, không tin context-window của grep.)

**Đã kiểm chứng (tự chạy lại trên cây NFD):**
- **estimate-only tái lập CHÍNH XÁC:** 50 calls · prompt 141.317 · output nominal 60.000 / cap 307.200 · total 201.317 / 448.517 · **cost $0.15532925 / $0.64972925** (tự tính: 141317/1e6·0.25 + 60000/1e6·2 = 0.15532925 ✓; cap 0.64972925 ✓). Model config in ra = config v7 verbatim (gpt-5.4-mini/temp1.0/seed20260612/none/low/6144).
- **0 API + 0 side-effect:** `--estimate-only` thoát TRƯỚC khi tạo `LLMClient`/đọc key; **KHÔNG tạo** thư mục `data/reports/builder_v2_c2_pilot` (verify: không tồn tại sau lệnh). Frozen hash `DA0F687894090D43` trước=sau; `git data` sạch.
- **Tests:** `test_builder_v2_pilot` **5 passed**; toàn bộ builder_v2 (consolidate7+render5+pilot5+concept_key6) **23 passed**. Mock-only (FakeTransport) — KHÔNG gọi API thật trong test, và phủ đúng 6 điểm siết.
- **Đọc code xác nhận 6 guard (CodeX #1–6) đã vào thật:** cache→`out_dir/llm_cache.sqlite3` (line 115), KHÔNG đụng frozen (mở `mode=ro` line 476); `_entry_surfaces` quét MỌI `source_variants[].surface`+canonical (#5, line 554); re-ask `bypass_cache=True`→`status=degraded` (#6, line 232/278); cost-gate kiểm `cost_usd_cap` (trần $0.65, bảo thủ) > ceiling → raise (line 182); estimate lấy từ C1 report KHÔNG chạy sổ rỗng (#4, line 161); key `OPENAI_API_KEY` env-first→file, không log (line 600).
- **Tests không nông:** cache-hit test chạy trên cache DB RIÊNG (miss×2 lần đầu, hit×2 lần sau); parse-fail test ra `degraded`+count=2+skip=1 không crash; cost-gate test raise đúng; estimate test ép dùng 141317 dù notebook rỗng.

**🟡 Ghi nhận trước khi chạy thật (cho user + bước C2-real):**
- **141.317 là OVER-estimate bảo thủ:** nó tính từ pack-proxy-v1 (C1). Run thật notebook RỖNG lớn dần ⇒ pack đầu chương nhỏ hơn ⇒ prompt thật **sẽ THẤP hơn** → tiền thật ≤ $0.155. Đúng hướng (ước cao, tiêu thấp).
- **Driver chưa gồm metrics so-sánh §17.5** (entry vs v1, recall-vs-gold, đo stoplist thật, số-ít/nhiều): nó chỉ chạy loop + ghi artifact (notebook/decision/cost/per_window). Phần CHẤM v2-vs-v1 + recall-vs-gold là **bước RIÊNG sau run thật** (đọc artifact + `builder_gold.score_builder_vs_gold`). KHÔNG block estimate-only, nhưng phải làm để C2 ra kết luận. → ghi vào C2-real.
- **`--c1-render-report` mặc định trỏ `data/reports/builder_v2_c1_render/...` (gitignore).** Nếu file vắng → fallback `50×6000=300k` prompt (ước CAO hơn → vẫn an toàn). Run thật nên regenerate report C1 trước để ước sát.

**Cost trình user duyệt:** **~$0.16 thực tế / trần $0.65** cho 1 chương `preliminaries`. Rẻ. Sau khi user OK ceiling → CodeX chạy `--confirm-usd <ceiling>` (đề xuất ceiling $0.70 để ôm trần) + bước metrics §17.5.
## 20. §5 — CodeX implementation notes Stage C2 real-run + metrics *(CodeX, 2026-06-29; STOP, no commit/push)*

**Scope implemented:** C2 real pilot for `preliminaries` after user approved cost; metrics §17.5 implemented and run. No production DB writes, no migration, no commit/push.

Additional files/changes after estimate-only review:
- `pipeline/scripts/builder_v2_pilot.py`
  - Archives full prompt for every window under `data/reports/builder_v2_c2_pilot/prompts/*.txt` before the API call. Each file contains full `SYSTEM` + `USER` message sent to the model.
  - `per_window_audit.json` includes `prompt_file` for lookup.
  - `raw_outputs.json` stores parsed four-bucket output per applied window for later audit/metrics.
  - `cost_log.json` records every attempt, including re-ask attempts if parse fails.
  - Key lookup fixed for running from `THESIS_RUNTIME_TOOL`: env first, then local/parent `OPENAI-KEY-*.txt`; key contents are never printed/logged.
- `pipeline/scripts/builder_v2_metrics.py`
  - Offline metric script; reads artifact + frozen DB `mode=ro`; writes `builder_v2_c2_metrics.json`; does not write DB.
  - Scope-matches v1 to the same chapter by evidence block ids.
  - Scores v2 notebook against eval-only gold by all source variants while counting actual notebook entries separately.
  - Distinguishes v1 separate-entry number splits from v2 merged source-variant groups (merged variants are desired, not duplicate entries).

Run notes:
- First real attempt auto-selected `OPENAI-KEY-1.txt` and failed on the first call with `insufficient_quota`; no model output was written.
- Real run was re-executed with `OPENAI-KEY-2.txt` set via environment variable for this process only; key contents were not printed/logged.

Real pilot command:
- `python pipeline\scripts\builder_v2_pilot.py --chapter preliminaries --confirm-usd 0.70 --out data\reports\builder_v2_c2_pilot`

Pilot result:
- status: **passed**
- windows: **50**
- applied_windows: **50**
- skipped_windows: **0**
- parse_failure_count: **0**
- cache_hits/cache_misses: **0 / 50**
- notebook_entries: **340**
- rejected_stoplist: **3**
- conflicts: **38**
- actual_cost_usd: **0.13093375**
- prompt archive files: **50**
- artifacts: `notebook.json`, `decision_log.json`, `cost_log.json`, `per_window_audit.json`, `raw_outputs.json`, `builder_v2_c2_pilot_report.json`, `prompts/`, `llm_cache.sqlite3`

Metrics command:
- `python pipeline\scripts\builder_v2_metrics.py --chapter preliminaries --artifact-dir data\reports\builder_v2_c2_pilot`

Metrics result (`builder_v2_c2_metrics.json`):
- entry_counts_scope_matched:
  - v1_chapter_terms: **381**
  - v2_notebook_entries: **340**
  - v2_rejected_stoplist: **3**
- recall-vs-gold DEV (gold_terms_present **57**):
  - v1: matched **36**, recall **0.631579**, agreement **0.805556**
  - v2: matched **38**, recall **0.666667**, agreement **0.605263**
- number-variant handling:
  - v1_separate_entry_groups: **29**
  - v2_duplicate_entry_groups: **0**
  - v2_merged_source_variant_groups: **67**
- conflicts by type:
  - bad_existing_target: **13**
  - canonical_target_change: **7**
  - plural_only_difference: **1**
  - polysemy_suspected: **7**
  - termhood_suspected: **8**
  - uncertain: **2**
- occurrence_conserved: **true**

Verification:
- `python -m pytest pipeline\tests\test_builder_v2_consolidate.py pipeline\tests\test_builder_v2_render.py pipeline\tests\test_builder_v2_pilot.py -q --basetemp D:\temp\pytest-builder-v2-all` → **17 passed**
- `python -m py_compile pipeline\scripts\builder_v2_pilot.py pipeline\scripts\builder_v2_metrics.py` → pass
- Frozen DB `data/jobs/d2l_p1/memory.sqlite3` SHA256 remains `DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8`.

Interpretation guard:
- Run quality is **mixed**. C2 reduces same-chapter entry count (381→340), eliminates duplicate concept-key entries for number variants (29→0), and increases DEV recall slightly (36→38 / 57). However DEV agreement drops (0.805556→0.605263) and conflicts are non-trivial (38). Do **not** claim Builder v2 is better until Claude re-derives metrics and inspects conflict/missing samples.

**STOP condition honored:** real API pilot completed, metrics completed, no production DB write, no commit, no push.

## 21. §6 — Claude review Stage C2 real-run + metrics *(Claude, 2026-06-29, re-derived ĐỘC LẬP trên artifact thật)*

**Verdict: PASS — cơ chế chạy đúng & số tái lập; nhưng KHÔNG được kết luận "v2 dịch tốt/tệ hơn v1" từ agreement. v2 THẮNG RÕ về cấu trúc (recall, phình, trùng số ít/nhiều, cost); "agreement tụt" một PHẦN là thiên lệch thước (any-match thưởng cho variant-bloat của v1), một PHẦN là chi phí thật của single-canonical — nhưng các ca lệch hầu hết là ĐỒNG NGHĨA hợp lệ, không phải dịch sai.** Cần judge mù để chốt chất lượng.

**Tái lập (tự tính trên artifact, không tin report):**
- Run: 50/50 windows, parse_fail=0, status=passed; cost **$0.13093375** (sum cost_log, 50/50 cache-miss = call thật); notebook **340** entries, rejected **3**, conflicts **38** (bad_existing_target 13, polysemy 7, canonical_change 7, termhood 8, uncertain 2, plural 1); occurrence conserved=true; frozen hash `DA0F687894090D43` bất biến; metrics script re-run khớp committed; 17 tests pass.
- **Recall (thước CÔNG BẰNG — cùng mẫu gold 57):** V1 36/57=**0.6316** → V2 38/57=**0.6667**. **v2 bắt được NHIỀU hơn.** ✓
- Entries 381→**340**; number-variant dup groups 29→**0**; v2 merged-source-variant groups **67** (gộp số-ít/nhiều hoạt động). ✓

**🔬 Mổ "agreement 0.806→0.605" (điểm phải soi):**
- agreement = (của các term BẮT được, bao nhiêu có target ∩ gold). Mẫu số KHÁC nhau (v1 matched 36, v2 38) và là **any-match** với TẬP target của builder.
- **Số biến thể chấp nhận TB/term: V1=4.36, V2=1.71** (V1 36/36 entry có >1 biến thể; V2 chỉ 24/38). → v1 (builder phình cũ) ôm một *túi* ~4 bản dịch/term nên "trúng" gold dễ; v2 cam kết 1 canonical (đúng thiết kế).
- **Thước CÔNG BẰNG canonical-vs-canonical (mỗi bên 1 target):** V1 26/36=**0.7222**, V2 23/38=**0.6053**. → khoảng cách any-match (0.806 vs 0.605 = 0.20) co còn (0.722 vs 0.605 = **0.12**) khi bỏ variant-bloat. Tức ~0.08 là **thiên lệch thước**, ~0.12 là **thật**.
- **8 ca v1-đúng / v2-không** soi tay: **5 sai-target nhưng là ĐỒNG NGHĨA/CHÍNH TẢ hợp lệ** — `vectơ` vs gold `vector` (cùng từ), `vô hướng` vs `số vô hướng` (gần trùng), `phân phối kết hợp` vs `đồng thời` (đồng nghĩa), `suy biên` vs `lấy biên`, `minibatch` dịch-vs-giữ-Anh; **3 MISS hẳn** (`agent`/`data`/`layer` — nhiều khả năng spillover liên-chương trong evidence v1, không phải khái niệm preliminaries; recall tổng vẫn nghiêng v2).

**Kết luận trung thực (validity):** agreement gold-any-match **KHÔNG phải head-to-head công bằng về chất lượng** — nó thưởng cho variant-bloat (đúng thứ v2 diệt) và gold D2L chỉ liệt 1 dạng trong nhiều dạng hợp lệ (đã ghi nhận gold không hẳn đủ/đúng). v2 không hề dịch SAI; nó cam kết 1 bản dịch hợp lệ khác dạng gold = **chi phí precision của tính nhất quán** (memory `memory-injection-precision-cost`, `occurrence-weighted-block-anymatch-inflation`). **v2 thắng cấu trúc; chất lượng dịch = CHƯA kết luận được, cần judge mù reference-aware trên ~15 ca v2-lệch.**

**Khuyến nghị bước sau:** (1) **KHÔNG** tuyên bố v2 tốt/tệ hơn về dịch dựa trên agreement; báo cáo cả any-match + canonical-only + nhãn "gold-incomplete-biased". (2) Judge mù (EV-06 kiểu) trên ~15 ca v2-lệch để biết "khác" hay "tệ". (3) Soi 3 miss (`agent`/`data`/`layer`): có thật thuộc preliminaries không hay spillover v1. (4) Nhớ: DEV, 1 chương, temp=1 = **1 mẫu** — đừng tổng quát. (5) 38 conflict = v2 tự gắn cờ nghi ngờ (theo thiết kế, không tự sửa) — input tốt cho review thủ công/Phase D.

## 22. Stage C3 — Term-Auditor (tầng 2 precision), PILOT/GIẢ-THUYẾT cần đo *(Claude spec; CodeX góp ý đã gộp; CodeX implement §5)*

**Bối cảnh:** C2 cho thấy v2 thắng cấu trúc nhưng cuốn 340 còn **trích dư** (~46% mục xuất hiện 1 lần; từ vu vơ `angle`/`circle`/`help`). User chốt nguyên tắc: **code KHÔNG làm việc ngôn ngữ; termhood phải do PROMPT/LLM**. Tham khảo (Claude+CodeX verify): ATE = bài toán tạo *candidate* rồi lọc (survey [arXiv 2301.06767]); critic LLM tầng 2 hạ false-positive, +F1 0.04–0.25 nhưng paper là *qualitative coding* → **analog, không phải bằng chứng tuyệt đối** [arXiv 2601.09905]; weirdness/termhood = tần suất domain vs general corpus [TermSuite P16-4003]; glossary-for-MT [arXiv 2410.15690]. → **C3 là PILOT để ĐO, không thay Builder v2 ngay.**

### 22.0 Phân vai (chốt theo nguyên tắc user + nuance CodeX)
- **Builder tầng 1 (C2, KHÔNG đổi):** trích rộng, giữ recall.
- **Term-Auditor tầng 2 (MỚI, LLM):** phán đoán từng candidate là thuật-ngữ-cần-khóa hay từ vu vơ. **MỌI phán đoán ngôn ngữ ở đây.**
- **Code:** CHỈ (a) gộp mặt chữ số-ít/nhiều (`concept_key`), (b) **tính tín hiệu CƠ HỌC** (đếm token, occurrence, regex code/number) đưa cho auditor, (c) **áp nhãn** auditor trả về. Code KHÔNG quyết "phổ thông/generic".
- **GỠ stoplist 12-từ trong consolidation** (đó là *danh sách ngôn ngữ hardcode* — sai nguyên tắc). Giữ lại CHỈ tín hiệu cơ học (single-token? occ<N? match code/number pattern?) làm *hint*, không xoá cứng. *(Điểm này user chốt; CodeX muốn giữ guard cơ học — gộp: cơ học=hint OK, danh-sách-từ=bỏ.)*

### 22.1 Input cho Auditor (code dựng, 0 phán đoán ngôn ngữ)
Mỗi candidate từ notebook v2: `source_term`, `canonical_target_vi`, `occurrences_total`, `is_single_token`(cơ học), `is_multiword`(cơ học), `matches_code_or_number_pattern`(regex), `term_type`/`do_not_translate` (builder đề xuất), `status` (conflict_pending), **+ 1 đoạn evidence ngắn (câu chứa từ)** để auditor thấy NGỮ CẢNH (cần cho termhood/polysemy). Tín hiệu = *bằng chứng*, KHÔNG phải luật.

### 22.2 PROMPT Auditor `d2l_term_audit_v1` *(Claude thiết kế, CodeX VERBATIM)* — đề cương
SYSTEM (đề cương, byte sẽ chốt ở bản kế):
- Vai: thẩm định viên thuật ngữ cho dịch sách kỹ thuật EN→VI. Với mỗi candidate, quyết nó có cần KHÓA bản dịch nhất quán cả cuốn không.
- **Tiêu chí nguyên-tắc (KHÔNG liệt kê từ, tổng quát mọi sách):** "Thuật ngữ cần-khóa = tên một khái niệm/phương pháp/đối tượng/đại lượng chuyên ngành, mà nếu các phần sách dịch khác nhau sẽ gây rối/sai nghĩa. Từ vu vơ = từ vựng phổ thông mà mọi dịch giả giỏi tự dịch đúng, dù xuất hiện trong câu kỹ thuật." Phép thử: *"dịch lệch từ này giữa các chương có gây hại không?"* + *"từ này đặc-thù-chuyên-ngành hay phổ thông?"* (đây là trực giác *weirdness* — LLM tự biết).
- **An toàn recall (luật đủ-căn-cứ, theo paper):** khi KHÔNG chắc → `needs_human_review`, **không** `generic_word_drop`. Ưu tiên precision nhưng không giết nhầm.
- Output mỗi candidate (taxonomy CodeX): `keep_as_translate_term` · `preserve_token` (giữ Anh: API/lib/dataset/ký hiệu) · `generic_word_drop` · `phrase_too_descriptive` · `polysemy_or_context_dependent` · `needs_human_review` — kèm `reason` 1 mệnh đề (audit trail).
- Tín hiệu (occurrence/single-token…) là *hint*, quyết định là của bạn. **Mù gold** (không tham chiếu đáp án D2L).
- Batch nhỏ (20–30 candidate/call) để kiểm soát context + cost.

### 22.3 Code consolidation (cơ học, áp nhãn)
`generic_word_drop`/`phrase_too_descriptive` → bỏ khỏi glossary production, **lưu vào `audited_out` (audit trail, không mất dấu)**. `preserve_token` → `do_not_translate=true`. `polysemy…`/`needs_human_review` → giữ + flag status. `keep_as_translate_term` → giữ. Code KHÔNG tự quyết; chỉ **thực thi** nhãn.

### 22.4 Metrics (preliminaries = DEV; thiết kế A-PRIORI, KHÔNG tune theo gold chương này)
entries 340→? · **recall-vs-gold KHÔNG được tụt dưới SÀN** (đề xuất sàn = **≥ v1 0.6316**; lý tưởng ≥ v2 0.667 − biên) · agreement (any-match + canonical-only) có HỒI không · auditor drop theo nhãn (bao nhiêu nhiễu `angle/circle/help` bị bắt đúng) · cost/audited-candidate · audit trail đầy đủ. **Lý tưởng: validate trên 1 chương HELD-OUT** (tránh học tủ).

### 22.5 Model (mở — CodeX đúng: chưa chứng minh mini đủ để THẨM ĐỊNH)
Thử **mini trước** (rẻ). Nếu mini thẩm định kém (bỏ term thật / giữ nhiễu) → thử **model mạnh hơn CHỈ ở bước auditor**, trên held-out (memory `reasoning-effort-consumes-output-budget`: thử model mạnh trên held-out, không phải reasoning trên dev). **Không mặc định mini đủ.**

### 22.6 Guards + Pass conditions
0 DB write (artifact JSON) · frozen ro · mù-gold · prompt verbatim (bump version khi đổi byte) · key env-first không-log · cost-gate `--estimate-only`→`--confirm-usd` · cache DB riêng. **PASS khi:** precision/agreement TĂNG RÕ **VÀ** recall không dưới sàn **VÀ** mọi quyết định auditor có audit trail. **Nếu recall tụt dưới sàn → REWORK prompt, không ép.** Đây là **giả thuyết**, fail cũng là kết quả hợp lệ (ghi nhận).

**Quy trình:** Claude chốt byte prompt `d2l_term_audit_v1` (bản kế) → CodeX phản biện → CodeX điền §5 (driver auditor + code áp nhãn + `--estimate-only`) → STOP, KHÔNG gọi API tới khi user duyệt $ → Claude review §6 (tái tính metrics + đọc audit trail).

## 23. Stage C3 - KHOA: card schema + BYTE PROMPT `d2l_term_audit_v1` + wiring *(Claude, 2026-06-29; SUPERSEDE de cuong 22.2/22.3; rev.2 sau CodeX round-2)*

> Chot sau 2 vong thiet ke voi user + CodeX. Cac quyet dinh duoi day **ghi de** 22.2 (de cuong) va 22.3 (von ghi "generic_word_drop -> bo khoi glossary" - MAU THUAN voi tagger-not-deleter, huy).

### 23.0 Quyet dinh da chot
1. **Auditor = DAN NHAN + XEP HANG, KHONG XOA.** Khong entry nao bi xoa khoi registry. "Rac" -> ha tier; precision dat khi **nhoi pack** (term tier thap tu rung duoi budget). => recall bao toan theo cau truc. (recall-at-build, precision-at-inject.)
2. **Pilot = SOFT-ONLY, 0 hard-drop.** Chi ha tier; chi sau khi do **false-drop** moi bat hard-drop o production. *(CodeX r1 #5.)*
3. **San recall do tren PACK INJECTED duoi budget thuc**, KHONG o registry. 3 muc: gold in registry? gold lot pack o window chua no? truot vi budget hay vi auditor ha tier? *(CodeX r1 #1.)*
4. **Map theo `entry_id`**; uu tien stable notebook entry id NEU co. Notebook hien KHONG co id rieng => PILOT dung `concept_key` TAM THOI + signal `overmerge_suspected`. **KHONG coi `concept_key` la production ID lau dai**; Phase D phai them id on dinh. *(CodeX r2 #3.)*
5. **Evidence adaptive 1-2 cau, CHI `block_type='prose'`** (bo heading/label/code/math_block). Term KHONG co prose -> `evidence:[]` + `evidence_missing_reason`, Auditor dua vao signals (`code_or_symbol_like`). *(CodeX r1 #4 + r2 #5.)*
6. **Mu gold o muc code:** renderer CHI doc `blocks.text`; TUYET DOI khong cham `eval_glossary_gold`/`reference_eval_only`.
7. Taxonomy: `generic_low_value`/`descriptive_phrase`/`uncertain_low_conf` (= tier review, **KHONG cong nguoi luc chay**; nguoi chi xuat hien OFFLINE khi validate false-drop cho luan van).
8. **Prompt KHONG neu vi du tu cu the in-domain** (`example/one/area`...). Vi du token co the la term gold (vd D2L co the co `example`) => neu lay lam vi du "generic" se ep Auditor ha nham term that VA la nhiem benchmark vao prompt (tuning-on-test). Chi viet NGUYEN TAC. *(CodeX r2 #1; Claude CO Y khong mo gold de kiem - mo gold de sua prompt cung la tuning-on-test.)*
9. **`polysemy_or_context_dependent` la HIGH-VALUE, KHONG phai rac.** Map -> `priority_tier=medium`, `injection_action=context_sensitive_translate` (action MOI), va prompt ghi ro: **khong bao gio xep duoi `generic_low_value`**; term polysemy van phai lot pack (kem variants), gan co context-sensitive. *(CodeX r2 #2.)*

### 23.1 LOCKED card schema (code dung, 0 phan doan ngon ngu; caps cung)
```
entry_id            = concept_key (PILOT key tam; Phase D can stable id)
source_term         = canonical_source_term
surface_variants    = [surface...]  (cap <= 8)
builder_proposed_vi = canonical_target_vi          # MODEL note, NOT gold
builder_target_variants = [text...] (cap <= 2)     # MODEL note, NOT gold
_note               = "builder_proposed_vi/variants are MODEL-GENERATED notes, NOT gold/reference"
signals = { occurrences_total, chapter_spread, is_multiword, do_not_translate,
            has_conflict, n_target_variants, surface_flags[], overmerge_suspected }  # co hoc, HINT
evidence            = [<=2 prose snippets, <=~45 words each]   # adaptive: 2 neu has_conflict|n_target_variants>1, else 1
evidence_missing_reason = "no prose occurrence"    # CHI khi evidence == []
evidence_truncated  = bool
```
`overmerge_suspected` (co hoc): bat khi 1 surface_variant chua `=`/`$` HOAC math/code-ish ma KHONG chua tu goc (vd "one" gop "H = 0"); surface mo ta van chua tu goc ("shape (2, 3, 4)") KHONG bi flag.

### 23.2 BYTE PROMPT `d2l_term_audit_v1` *(Claude thiet ke - CodeX VERBATIM; bump version khi doi byte)*
```
[SYSTEM]
You are a terminology auditor for an English-to-Vietnamese translation memory of the
deep-learning textbook "Dive into Deep Learning" (D2L). An upstream extractor (the
"Builder") favored recall, so the candidate list mixes real domain terms with generic
words, code tokens, and over-long phrases. Your job is to LABEL each candidate so a later
step can decide which entries to prioritize in the translator's memory. You do NOT
translate, rewrite, or invent terms - you only judge and label what you are given.

Termhood principle (apply it; do NOT use any fixed word list):
- A CONTROLLED TERM names a domain concept (method, object, quantity, model, structure)
  whose inconsistent translation across the book would harm meaning or confuse the reader.
  It deserves a glossary entry.
- A GENERIC WORD is ordinary vocabulary (everyday nouns, verbs, connectives) that a
  competent translator renders correctly from context without a glossary, even inside a
  technical sentence. Judge by the role the word plays in the evidence, not by a fixed list.
- Decide from the evidence sentences and your domain knowledge - not from frequency alone.

Recall-safety (this matters): the memory's value is translation CONSISTENCY, so dropping a
real term is worse than keeping a generic one - a kept generic term may still fall out later
under budget, but a dropped real term is lost. When evidence is thin or you are genuinely
unsure, choose keep_as_translate_term or uncertain_low_conf - never a low-value label on a
hunch.

Reading the fields:
- builder_proposed_vi and builder_target_variants are the SYSTEM'S OWN EARLIER NOTES, NOT
  gold/reference translations; they may be wrong. Use them only as a hint. If the evidence
  shows the proposed translation is context-dependent or incorrect, that itself is a signal
  (often polysemy_or_context_dependent).
- signals (occurrences_total, chapter_spread, has_conflict, do_not_translate,
  n_target_variants, surface_flags, overmerge_suspected) are mechanical HINTS, not verdicts.
  Many conflicting renderings + divergent evidence -> suspect polysemy; surface_flags
  "code_or_symbol_like" or do_not_translate true -> suspect preserve_token;
  overmerge_suspected true means the surface set may mix more than one concept - judge the
  head term, do not let merged fragments mislead you.

Choose exactly one audit_label per entry:
- keep_as_translate_term - a genuine domain term to translate consistently.
- preserve_token - keep verbatim in English / as a symbol (code identifiers, library
  functions, file formats, proper nouns, math symbols).
- generic_low_value - ordinary vocabulary, not worth controlling.
- descriptive_phrase - a compositional/explanatory phrase (several words describing
  something), not a single lexical term to control.
- polysemy_or_context_dependent - two or more valid renderings depending on context;
  forcing one canonical would mislead. Do NOT pick a translation; flag it.
- uncertain_low_conf - genuinely uncertain after weighing the evidence.

Also set for each entry:
- priority_tier: high | medium | low | review
- injection_action: translate | preserve | context_sensitive_translate | deprioritize | review_only
- confidence: high | medium | low
- reason: one short clause (<= 20 words) naming the deciding evidence or signal.

Default label -> tier -> action (you MAY deviate, but say why in reason):
keep_as_translate_term         -> high   / translate
preserve_token                 -> high   / preserve
polysemy_or_context_dependent  -> medium / context_sensitive_translate
generic_low_value              -> low    / deprioritize
descriptive_phrase             -> low    / deprioritize
uncertain_low_conf             -> review / review_only

IMPORTANT: polysemy_or_context_dependent terms are HIGH-VALUE - they are exactly where
consistent, context-aware translation matters most. Never rank them below generic_low_value
or treat them as noise; they must still reach the translator (with their variants), flagged
for context-sensitive handling.

Output: a single JSON array, EXACTLY one object per input entry, keyed by entry_id, in the
same order, no extra entries, no commentary:
[{"entry_id":"...","audit_label":"...","priority_tier":"...","injection_action":"...","confidence":"...","reason":"..."}]

Judge only from each card. Do not request more context. Output nothing except the JSON array.

[USER]
Audit the following candidate term cards. Return the JSON array as specified.
<CARDS_JSON_ARRAY>
```

### 23.3 Card that - render bang LENH (reproducible trong repo, 0 API, read-only DB)
Script committed: `THESIS_RUNTIME_TOOL/pipeline/scripts/builder_v2_c3_sample_cards.py` (review-only reproducer; production card-builder van la 5 cua CodeX). Lenh:
```
python THESIS_RUNTIME_TOOL/pipeline/scripts/builder_v2_c3_sample_cards.py \
  --notebook THESIS_RUNTIME_TOOL/data/reports/builder_v2_c2_pilot/notebook.json \
  --db       THESIS_RUNTIME_TOOL/data/jobs/d2l_p1/memory.sqlite3 \
  --terms norm shape gradient one example arange linalg.norm circle
```
(Output JSON la artifact regenerable - gitignore data/reports; tai lap bang lenh tren.)

Polysemy (ca kho nhat - Auditor phai ra `polysemy_or_context_dependent`, tier medium):
```json
{
 "entry_id": "shape",
 "source_term": "shape",
 "surface_variants": [
  "shape",
  "shape (height, width)",
  "target shape",
  "shapes",
  "same shape",
  "shape becomes a square",
  "shape (2, 3, 4)"
 ],
 "builder_proposed_vi": "hình dạng",
 "builder_target_variants": [
  "kích thước"
 ],
 "_note": "builder_proposed_vi/variants are MODEL-GENERATED notes, NOT gold/reference",
 "signals": {
  "occurrences_total": 23,
  "chapter_spread": 1,
  "is_multiword": false,
  "do_not_translate": false,
  "has_conflict": true,
  "n_target_variants": 1,
  "surface_flags": [],
  "overmerge_suspected": false
 },
 "evidence": [
  "Reshaping by manually specifying every dimension is unnecessary. If our target shape is a matrix with shape (height, width), then after we know the width, the height is given implicitly. Why should we have to perform the division ourselves? In the example above, to get …",
  "… and yielding one output) by the signature $f: \\mathbb{R}, \\mathbb{R} \\rightarrow \\mathbb{R}$. Given any two vectors $\\mathbf{u}$ and $\\mathbf{v}$ *of the same shape*, and a binary operator $f$, we can produce a vector $\\mathbf{c} = F(\\mathbf{u},\\mathbf{v})$ by setting $c_i \\gets f(u_i, v_i)$ for all $i$, …"
 ],
 "evidence_truncated": true
}
```
Preserve (Auditor phai ra `preserve_token`):
```json
{
 "entry_id": "arange",
 "source_term": "arange",
 "surface_variants": [
  "arange",
  "`arange(n)`"
 ],
 "builder_proposed_vi": "arange",
 "builder_target_variants": [],
 "_note": "builder_proposed_vi/variants are MODEL-GENERATED notes, NOT gold/reference",
 "signals": {
  "occurrences_total": 2,
  "chapter_spread": 1,
  "is_multiword": false,
  "do_not_translate": true,
  "has_conflict": false,
  "n_target_variants": 0,
  "surface_flags": [
   "code_or_symbol_like"
  ],
  "overmerge_suspected": false
 },
 "evidence": [
  ":begin_tab:`mxnet` MXNet provides a variety of functions for creating new tensors prepopulated with values. For example, by invoking `arange(n)`, we can create a vector of evenly spaced values, starting at 0 (included) and ending at `n` (not included). By default, the interval size is $1$. …"
 ],
 "evidence_truncated": true
}
```
Phat hien phu: card `one` -> `overmerge_suspected:true` (Builder gop manh cong thuc "H = 0" vao "one"). Loi nay thuoc tang Builder, NGOAI pham vi C3; ghi nhan xu ly rieng.

### 23.4 Wiring (CodeX implement 5) - phai tac dong THAT vao injection
- **Luu ket qua**: moi entry them `{audit_label, priority_tier, injection_action, confidence, reason}` (keyed `entry_id`) -> notebook da-audit + `audit_trail.json`.
- **`preserve_token`** -> set `do_not_translate=true`.
- **`context_sensitive_translate`** (polysemy) -> nhoi pack KEM variants + co context-sensitive; **tier medium, KHONG xep duoi generic**.
- **Injection (PILOT = SIMULATE, KHONG dung frozen DB):** guong dung `context_builder._glossary_items()`: sort `(-count, source.casefold(), id)` (context_builder.py:266) + skip `occurrences < min_injection_occurrences` (context_builder.py:421). Auditor tier chen vao sort: `(tier_rank, -count, source)`; pilot KHONG them skip cung. Do recall-on-injected-pack o budget thuc.
- **Phase D:** wiring THAT vao `context_builder` + migration glossary + stable entry id (frozen DB RO => audit song o artifact). Pilot mo phong dung de so chuyen duoc.

### 23.5 Metrics + Pass (carry 22.4/22.6, sua san recall ve injected-pack)
entries 340->? · **recall-on-injected-pack KHONG duoi SAN** (de xuat >= v1 0.6316) · false-drop rate (tier thap chua bao nhieu gold) · noise-removed · agreement (any-match + canonical-only) co hoi khong · cost/audited-candidate · audit trail day du. **PASS:** precision/agreement TANG RO **VA** recall khong duoi san **VA** moi quyet dinh co audit trail. Recall duoi san -> REWORK prompt, khong ep. La GIA THUYET, fail cung la ket qua hop le. Model mo (mini truoc; kem -> model manh hon CHI o buoc auditor, tren held-out).

### 23.6 CodeX round-2 fixes (Claude NHAN ca 5)
1 bo vi du in-domain khoi prompt (anti-tuning-on-test) · 2 polysemy -> medium/context_sensitive_translate, khong duoi generic · 3 concept_key chi PILOT id + overmerge_suspected · 4 card render bang LENH committed (khong vien dan file ngoai repo) · 5 no-prose -> evidence:[] + evidence_missing_reason.

### 23.7 Quy trinh
Claude da khoa byte prompt (23.2 rev.2) + schema (23.1) + script render (23.3) -> **CodeX phan bien lai** -> CodeX dien 5 (driver auditor + code dung card dung schema 23.1, prose-only, mu 2 bang gold + ap nhan + `--estimate-only`) -> STOP, KHONG goi API toi khi user duyet $ -> Claude review 6.

## 24. §5 — CodeX implementation notes Stage C3 estimate-only *(CodeX, 2026-06-30; STOP, no commit/push)*

**Scope implemented:** C3 Term-Auditor card builder + prompt archive + cost estimate + audit-label apply/simulated-injection helper. **No real API was called.**

Files changed:
- `pipeline/prepass/builder_v2_audit.py`
  - Shared implementation for locked `d2l_term_audit_v1` prompt, card schema, prose-only evidence extraction, no-prose handling, `overmerge_suspected`, chunking, prompt rendering, audit-output validation, audit-label application, and simulated injection ordering.
  - Does not read `eval_glossary_gold` or `reference_eval_only`; card evidence is fetched only from `blocks.text` in frozen DB opened `mode=ro`.
- `pipeline/scripts/builder_v2_c3_sample_cards.py`
  - Thin 0-API reproducer now uses the shared production card builder instead of a duplicate preview implementation.
- `pipeline/scripts/builder_v2_c3_audit.py`
  - `--estimate-only` builds all 340 cards, chunks them, archives full prompts under `data/reports/builder_v2_c3_audit_estimate/prompts/*.txt`, writes `cards.json`, `chunks.json`, and `builder_v2_c3_audit_estimate.json`, then exits before any `LLMClient`/API-key path.
  - Chunking is token-budget aware: max card count is 40, but chunks split earlier at a **90% safety budget** (`prompt_token_budget=5400` under `prompt_token_cap=6000`; observed max **5394**).
  - `--audit-json` validates a pre-existing JSON-array audit result, applies labels into artifact-only `notebook_audited.json`, and writes `injection_preview.json`; this is for replay/apply only, not API.
- `pipeline/tests/test_builder_v2_audit.py`
  - Mock/offline tests for card evidence, no-prose guard, overmerge signal, audit validation, preserve-token application, tier-aware simulated injection sort, and estimate-only prompt archival without API/key.

Important implementation note:
- `d2l_term_audit_v1` output is a **JSON array**. C3 estimate-only archives the prompt exactly; the later real-call step must either parse raw JSON array output or choose an API response-format strategy that does not silently require a top-level JSON object. No real-call path was enabled in this §5.

Commands run:
- `python -m pytest pipeline\tests\test_builder_v2_audit.py -q --basetemp D:\temp\pytest-builder-v2-c3` → **4 passed**
- `python pipeline\scripts\builder_v2_c3_sample_cards.py --notebook data\reports\builder_v2_c2_pilot\notebook.json --db data\jobs\d2l_p1\memory.sqlite3 --out data\reports\builder_v2_c3_audit_estimate\sample_cards_v1.json` → wrote **8** sample cards
- `python pipeline\scripts\builder_v2_c3_audit.py --estimate-only --out data\reports\builder_v2_c3_audit_estimate` → **0 API**, output:
  - cards: **340**
  - calls/chunks: **17**
  - prompt_tokens_total: **87812**
  - prompt_tokens_max: **5394** (under safety budget 5400 / cap 6000)
  - estimated_output_tokens_nominal: **32640**
  - estimated_output_tokens_cap: **104448**
  - estimated_cost_usd_nominal: **0.087233**
  - estimated_cost_usd_cap: **0.230849**
  - db_hash_unchanged: **true**
- `python -m pytest pipeline\tests\test_builder_v2_consolidate.py pipeline\tests\test_builder_v2_render.py pipeline\tests\test_builder_v2_pilot.py pipeline\tests\test_builder_v2_audit.py -q --basetemp D:\temp\pytest-builder-v2-c123` → **21 passed**

Artifacts (gitignored under `data/reports/builder_v2_*`):
- `data/reports/builder_v2_c3_audit_estimate/cards.json`
- `data/reports/builder_v2_c3_audit_estimate/chunks.json`
- `data/reports/builder_v2_c3_audit_estimate/prompts/chunk_001.txt` … `chunk_009.txt`
- `data/reports/builder_v2_c3_audit_estimate/builder_v2_c3_audit_estimate.json`
- `data/reports/builder_v2_c3_audit_estimate/sample_cards_v1.json`

**STOP condition honored:** estimate-only only; no API, no commit, no push.

## 25. §5 — CodeX implementation notes Stage C3 real-run + injected-pack metric *(CodeX, 2026-06-30; STOP, no commit/push)*

**Scope implemented:** opened the C3 real API path after user cost approval, ran all 340 Auditor cards, wrote artifact-only audited notebook, and added an eval-only recall-on-injected-pack metric that mirrors the production injection path more closely than the earlier preview. **No production DB write.**

Files changed since §24:
- `pipeline/scripts/builder_v2_c3_audit.py`
  - Added guarded real-run mode: `--confirm-usd <amount>` is required unless `--estimate-only` or `--audit-json` is used.
  - Gate reruns the same estimate first and refuses if the cap estimate exceeds the confirmation amount.
  - Uses a separate cache DB (`<out>/llm_cache.sqlite3`) and does not touch frozen `memory.sqlite3`.
  - Key loader is env-first, then KEY-2 before KEY-1; report only records key source label, never the key.
  - Parses the locked prompt's raw JSON-array output directly. If parsing/schema validation fails, it re-asks once; repeated failure marks the run `degraded`.
  - Writes `cost_log.json`, `raw_outputs.json`, `audit_trail.json`, `notebook_audited.json`, `injection_preview.json`, and `builder_v2_c3_injected_pack_metrics.json`.
  - Injected-pack metric is eval-only: Auditor/card path remains blind to gold; metric reads `eval_glossary_gold` only after audit.
  - Hardening: `simulate_injection_order` now receives `min_injection_occurrences` from `PROFILES["technical_d2l_v1"]` when not explicitly overridden; no hardcoded CLI default.
- `pipeline/tests/test_builder_v2_audit.py`
  - Added fake-transport real-run helper test for invalid JSON -> one re-ask -> valid JSON, with cost/raw-output logs and zero API.

Metric mirror implemented:
- Builds translation windows with `build_windows(..., block_types=PROFILES["technical_d2l_v1"].translatable_block_types)`.
- Creates eligible rows from `notebook_audited.json`, then applies `term_is_injection_eligible()` / `injection_role_for_term()` from `pipeline.translate.profiles`.
- Matches anchors by all source surfaces against each window.
- Sorts by Auditor tier first, then in-window count, total occurrence count, source, id.
- Cuts by the real S1 context budget (`--context-budget`, default 500) using the same rough token estimator family as the prompt tooling.
- This is still a simulation over artifact notebook, not a production `glossary_entries` migration; Phase D must wire the same fields into `context_builder` before production use.

Commands run:
- `python -m pytest pipeline\tests\test_builder_v2_audit.py -q --basetemp D:\temp\pytest-builder-v2-c3-audit` -> **5 passed**
- `python -m pytest pipeline\tests\test_builder_v2_consolidate.py pipeline\tests\test_builder_v2_render.py pipeline\tests\test_builder_v2_pilot.py pipeline\tests\test_builder_v2_audit.py -q --basetemp D:\temp\pytest-builder-v2-c3-suite` -> **22 passed**
- `python pipeline\scripts\builder_v2_c3_audit.py --estimate-only --out data\reports\builder_v2_c3_audit_real` -> estimate gate:
  - cards: **340**
  - calls/chunks: **17**
  - prompt_tokens_total: **87812**
  - estimated_output_tokens_nominal: **32640**
  - estimated_output_tokens_cap: **104448**
  - estimated_cost_usd_nominal: **0.087233**
  - estimated_cost_usd_cap: **0.230849**
  - db_hash_unchanged: **true**
- `python pipeline\scripts\builder_v2_c3_audit.py --confirm-usd 0.231 --out data\reports\builder_v2_c3_audit_real`:
  - First real run populated the separate cache, then the new metric failed on a schema assumption (`blocks.block_index` absent in frozen DB; actual column is `order_index`). Fixed metric fetch and reran the same command; second run replayed the 17 cached Auditor responses and completed. No extra API calls on the rerun.
  - final status: **completed**
  - parse_failure_count: **0**
  - API key source: **file:OPENAI-KEY-2.txt**
  - actual Auditor cost recorded: **$0.0468833**
  - final rerun cost log: **17 cache hits / 0 cache misses** (because the first real run had already cached responses)
  - prompt_tokens billed/recorded in cache: **78730**
  - completion_tokens: **14090**
  - frozen DB hash unchanged: **DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8**

Auditor label distribution (`audit_trail.json`, 340 cards):
- `keep_as_translate_term`: **201**
- `polysemy_or_context_dependent`: **32**
- `preserve_token`: **26**
- `generic_low_value`: **47**
- `descriptive_phrase`: **31**
- `uncertain_low_conf`: **3**

Injected-pack metric result (`builder_v2_c3_injected_pack_metrics.json`):
- entry_counts:
  - registry entries before audit: **340**
  - production eligible after profile rules: **167**
  - unique entries that actually enter at least one simulated window pack: **149**
- recall-vs-gold DEV:
  - gold_terms_present: **57**
  - registry_before_budget: matched **38/57**, recall **0.666667**, agreement **0.605263**
  - injected_pack: matched **29/57**, recall **0.508772**, agreement **0.655172**
  - floor_v1: **0.6316**
  - pass_floor: **false**
- false-drop gold hits among low-value labels: **4**
  - `concatenate` -> `generic_low_value` ("standard verb, context determines rendering")
  - `data manipulation` -> `generic_low_value` ("compositional phrase, not fixed term")
  - `example` -> `generic_low_value` ("ordinary discourse marker and illustration")
  - `framework` -> `generic_low_value` ("generic software platform word")

Interpretation for reviewer:
- C3 real-run mechanics PASS: full 340 audited, no parse failures, cost below cap, cache separate, frozen DB unchanged, artifacts complete.
- C3 quality hypothesis **does not pass as configured**: recall-on-injected-pack is **0.508772**, below floor **0.6316**, and the smoke concern around `example` is confirmed as a real false-drop.
- The main recall loss is not only Auditor tiering; production eligibility (`min_injection_occurrences=2`, preserve exclusion) and per-window budget also remove gold terms. This is the requested production-path mirror, but it means the earlier registry-level floor is not an apples-to-apples injected-pack floor.
- Do **not** claim C3 improves production memory yet. Claude should review whether to (a) change evidence selection / prompt for false-drop terms, (b) adjust injection policy for low-frequency gold terms, or (c) redefine the floor for production-injected pack vs registry-level recall.

Artifacts (gitignored under `data/reports/builder_v2_*`):
- `data/reports/builder_v2_c3_audit_real/cards.json`
- `data/reports/builder_v2_c3_audit_real/chunks.json`
- `data/reports/builder_v2_c3_audit_real/prompts/chunk_001.txt` ... `chunk_017.txt`
- `data/reports/builder_v2_c3_audit_real/llm_cache.sqlite3`
- `data/reports/builder_v2_c3_audit_real/cost_log.json`
- `data/reports/builder_v2_c3_audit_real/raw_outputs.json`
- `data/reports/builder_v2_c3_audit_real/audit_trail.json`
- `data/reports/builder_v2_c3_audit_real/notebook_audited.json`
- `data/reports/builder_v2_c3_audit_real/injection_preview.json`
- `data/reports/builder_v2_c3_audit_real/builder_v2_c3_audit_estimate.json`
- `data/reports/builder_v2_c3_audit_real/builder_v2_c3_injected_pack_metrics.json`

**STOP condition honored:** real-run complete, §5 filled, no commit, no push.

## 26. §5 — CodeX implementation notes Stage C3 metric fix *(CodeX, 2026-06-30; STOP, no commit/push)*

**Scope implemented:** fixed the C3 Auditor metric to measure the Auditor's own recall cost on the dictionary, not a Translator injected-pack budget KPI. No prompt/card-builder bytes changed. No API calls. Existing `audit_trail.json` was replayed.

Files changed since §25:
- `pipeline/scripts/builder_v2_c3_audit.py`
  - Added eval-only `builder_v2_c3_auditor_metrics.json`.
  - Metric A = Builder registry recall vs gold for the chapter, with no Translator budget and no occurrence filter.
  - Metric B = same registry after applying Auditor labels as a dictionary filter: drop `generic_low_value` + `descriptive_phrase`; keep `keep_as_translate_term`, `preserve_token`, `polysemy_or_context_dependent`, `uncertain_low_conf`.
  - `delta = A - B` is the true Auditor recall cost.
  - False-drop attribution now counts only gold terms present in Metric A and removed in Metric B by Auditor drop labels.
  - Exports `keep_as_translate_term_terms` (201 rows) and `terms_by_label` for Claude's manual false-keep review. Code does not claim precision/noise success.
- `pipeline/prepass/builder_v2_audit.py`
  - `simulate_injection_order()` default `min_injection_occurrences` changed **2 -> 0** so helper defaults no longer preserve the old crude occurrence filter. Tests that need the old scenario pass it explicitly.
- `pipeline/translate/profiles.py`
  - `technical_d2l_v1.min_injection_occurrences` changed **2 -> 0**. This is an intentional design change: Auditor labels become the semantic precision gate; the old occurrence threshold killed low-frequency gold terms. This changes S1 injection behavior and requires Claude review before production translation.
- `pipeline/tests/test_d2l_translate_score.py`
  - Updated expectations for the new no-occurrence-filter behavior: fixture term `exposes` (`occurrences_count=1`) now enters registry injection/adherence denominators and scores 0 in the fixture because its target is absent.

Commands run:
- `python pipeline\scripts\builder_v2_c3_audit.py --audit-json data\reports\builder_v2_c3_audit_real\audit_trail.json --out data\reports\builder_v2_c3_audit_real` -> replayed existing audit trail, **0 API**, output summary:
  - status: **applied_existing_audit**
  - zero_api: **true**
  - frozen DB hash unchanged: **true**
  - Metric A registry recall: **38/57 = 0.666667**
  - Metric B post-Auditor recall: **34/57 = 0.596491**
  - Auditor recall delta: **0.070176** (4 gold terms)
- `python -m pytest pipeline\tests\test_builder_v2_audit.py pipeline\tests\test_d2l_translate_score.py -q --basetemp D:\temp\pytest-builder-v2-c3-metric-fix` -> **21 passed**

Auditor label distribution (`audit_trail.json`, 340 cards):
- `keep_as_translate_term`: **201**
- `polysemy_or_context_dependent`: **32**
- `preserve_token`: **26**
- `generic_low_value`: **47**
- `descriptive_phrase`: **31**
- `uncertain_low_conf`: **3**

Metric result (`builder_v2_c3_auditor_metrics.json`):
- Gold denominator: **57** source terms present in the preliminaries source text.
- Entry counts:
  - registry entries: **340**
  - post-Auditor kept entries: **262**
  - post-Auditor dropped entries: **78**
- Metric A, dictionary before Auditor filtering:
  - matched **38/57**, recall **0.666667**, agreement **0.605263**
- Metric B, dictionary after Auditor filtering:
  - matched **34/57**, recall **0.596491**, agreement **0.647059**
- Delta:
  - **0.070176** recall cost, exactly 4 gold terms.

False-drop list (correct attribution: matched in A, removed by Auditor label):
| source_term | occ | label | reason | gold_target |
|---|---:|---|---|---|
| `concatenate` | 2 | `generic_low_value` | standard verb, context determines rendering | nối |
| `data manipulation` | 1 | `generic_low_value` | compositional phrase, not fixed term | thao tác với dữ liệu |
| `example` | 30 | `generic_low_value` | ordinary discourse marker and illustration | mẫu |
| `framework` | 2 | `generic_low_value` | generic software platform word | framework |

Precision/noise note:
- CodeX does **not** auto-score precision/noise removal. Artifact now contains the full `keep_as_translate_term_terms` list (**201 rows**) plus all terms grouped by label/reason for Claude to inspect false-keeps manually.

Artifact note:
- New authoritative metric file: `data/reports/builder_v2_c3_audit_real/builder_v2_c3_auditor_metrics.json`.
- Older `builder_v2_c3_injected_pack_metrics.json` remains a superseded generated KPI artifact only. It must not be used as C3 Auditor pass/fail.

**STOP condition honored:** metric fix replay complete, 0 API, no commit, no push.

## 27. Stage C3.5 - De-collision pass: sua canonical dung do (recall-safe, mu gold) *(Claude, 2026-06-30; rev.2 ap CodeX 6-diem review)*

> Truy nguon da xong (memory `consolidation-ignores-bad-existing-target`). Byte prompt `d2l_decollision_v1` (27.3) do Claude thiet ke - CodeX implement VERBATIM. CodeX lam code/wiring, dien 5, STOP khong commit; Claude review + commit. **rev.2 = ap 6 diem siet cua CodeX (validator distinct, candidate provenance, normalize giu dau, metric recall-vs-agreement, polysemy khong ro ri canonical cu, scope-honesty).**
>
> **SYNC:** §27 da nam trong HEAD `e991c95`. Neu workspace ai do ket thuc o §26 -> sync ve commit nay truoc khi chay.

### 27.0 Boi canh (da truy nguon, dung lam lai)
- Loi `gradient -> "dao ham rieng"` **KHONG phai Builder dich sai** - Builder da gan co `bad_existing_target` 4 lan kem de xuat dung; **consolidation bo quen ledger** (giu canonical cu sai khi co nhieu de xuat choi nhau).
- Ban kinh loi that ~= **2 tu**: `gradient` (ledger-flagged) + `product rule` (dung-do-im-lang, ledger rong).
- **Ca hai deu DUNG DO cheo-entry**: gradient<->partial derivative tren `"dao ham rieng"`; product rule<->multiplication rule tren `"quy tac nhan"`. => **mot bo do dung-do + mot luot LLM de-collision bat duoc ca hai, KHONG can chay lai luot Auditor chinh.**
- Tin hieu `canonical not-in target_variants` (144 ca) la **nhieu**, KHONG dung lam bo do.

### 27.1 Bat bien (phai giu)
- **Mu gold tuyet doi** o tang card + prompt: card builder CHI doc `blocks.text`, **khong cham** `eval_glossary_gold`/`reference_eval_only`. Metric moi duoc doc gold (eval-only).
- **LLM chi CHON trong candidates, KHONG bia** ban dich moi.
- **Soft-only, khong xoa entry nao** - chi relabel / repick canonical. Recall bat bien theo cau truc.
- Frozen DB hash `DA0F...D464B8`, mode=ro. OPENAI key env-first, **khong log**.
- **STOP sau khi dien 5, KHONG commit, KHONG push.**

### 27.2 Pipeline (luot 2, sau luot Auditor chinh)
**Buoc 1 - Bo do dung do (code, may moc).** Tren cac entry DUOC GIU (`audit_label in {keep_as_translate_term, preserve_token, polysemy_or_context_dependent, uncertain_low_conf}`), nhom cac entry co `canonical_target_vi` **chuan hoa trung nhau** ma `source_term` khac nhau -> moi nhom >=2 thanh vien.
- **Chuan hoa (CodeX #4):** `NFC` + `strip` + collapse whitespace (`\s+`->1) + `casefold`. **GIU DAU tieng Viet va "d/d"** - tuyet doi **KHONG dung `_normalize_vi`** (da verify: no bo dau thanh + d->d => gop nham "bien"/"bien"). Dung mot helper rieng giu-dau.
- Khong phan nghia - chi gom theo chuoi da chuan hoa (nhom dong-nghia-lanh-tinh cung gom, de LLM xu).

**Buoc 2 - Card "nhom dung do" (code, mu gold).** Moi nhom -> 1 object; moi thanh vien:
```
entry_id          = concept_key
source_term       = canonical_source_term
shared_canonical  = canonical_target_vi (cai dang trung)
candidates        = [ {text, source, type} ... ]   # CodeX #3: provenance co hoc, cap <= 6, dedup theo text
                    # source in {target_variant, conflict_ledger}
                    # type    = ledger type neu tu ledger (bad_existing_target/canonical_target_change/polysemy_suspected), else null
                    # gop tu target_variants[].text  va  conflict_ledger[].proposed_target
evidence          = [<=2 prose snippet, ~45 tu, CHI block_type='prose'], uu tien cau chua term
signals           = { occurrences_total, builder_conflict_note: bool(conflict_ledger non-empty) }
```
> Vi du gradient: candidates = [{gradient, conflict_ledger, bad_existing_target}, {dao ham, conflict_ledger, canonical_target_change}, {do doc, conflict_ledger, polysemy_suspected}, {dao ham theo huong, target_variant, null}].

### 27.3 BYTE PROMPT `d2l_decollision_v1` *(Claude thiet ke - CodeX VERBATIM; chua tung chay nen rev pre-implementation, khong can bump)*
```
[SYSTEM]
You resolve naming COLLISIONS in an English-to-Vietnamese translation memory for the
deep-learning textbook "Dive into Deep Learning" (D2L). Upstream code has detected GROUPS:
each group is a set of DISTINCT English source terms that were assigned the SAME Vietnamese
canonical translation. For each member you decide whether that shared translation is correct,
or whether the terms are actually different concepts that must get distinct translations, or
whether a term is context-dependent. You do NOT translate from scratch and you do NOT invent
new Vietnamese wordings - you only choose among the candidates you are given, or flag.

Hard rules:
- Choose a canonical ONLY from the "candidates" list provided for that term (use its "text").
  If none fits, do NOT invent one - use mark_polysemy or uncertain.
- For resolve_distinct, the chosen canonical MUST differ from shared_canonical, AND must differ
  from any sibling you leave at keep_shared - otherwise the collision is NOT removed.
- You are given no reference or gold translation; do not assume one exists. Judge from the
  evidence sentences and your own domain knowledge.
- Within one group, two members you both resolve as distinct must NOT end up with the same
  canonical.
- Never drop or delete a term; you only relabel or re-pick its canonical.

Recall-safety: a wrong forced translation is worse than an honest "context-dependent" flag.
When evidence is thin or the term genuinely has several valid renderings, prefer
mark_polysemy. Assign a distinct canonical only when the evidence clearly shows the terms are
different concepts.

Reading each member:
- source_term: the English term.
- shared_canonical: the Vietnamese translation currently shared with the other members.
- candidates: the ONLY Vietnamese wordings you may choose from. Each has a "text" plus a
  mechanical provenance ("source"/"type"): a candidate from "conflict_ledger" with type
  "bad_existing_target" or "canonical_target_change" is the upstream extractor's OWN flag that
  the shared name is wrong for this term - weigh it as a strong hint, but still confirm from the
  evidence. A "target_variant" candidate is merely another rendering seen for this term.
- evidence: 1-2 source sentences showing how the term is used (use these to tell concepts apart).
- signals: occurrences and an optional upstream note that the translation was flagged
  inconsistent - a hint, not a verdict.

Choose exactly one decision per member:
- keep_shared: the shared_canonical is correct for this term. (If ALL members keep_shared, the
  group was a benign true-synonym group.)
- resolve_distinct: pick from candidates a canonical that differs from the colliding siblings,
  because this is a distinct concept.
- mark_polysemy: the term has two or more valid renderings depending on context; do not force
  one (set chosen_canonical to null).
- uncertain: genuinely unsure after weighing the evidence (set chosen_canonical to null).

Also set:
- chosen_canonical: keep_shared -> the shared_canonical; resolve_distinct -> the "text" of one
  candidate (must differ from shared_canonical); mark_polysemy / uncertain -> null.
- confidence: high | medium | low
- reason: one short clause (<= 20 words) naming the deciding evidence.

Output: a single JSON array, EXACTLY one object per input member, keyed by entry_id, in the
same order, no commentary:
[{"entry_id":"...","decision":"...","chosen_canonical":"... or null","confidence":"...","reason":"..."}]

Judge only from what you are given. Output nothing except the JSON array.

[USER]
Resolve the following collision groups. Return the JSON array as specified.
<GROUPS_JSON>
```

### 27.4 Validator (code, nhu `validate_audit_results`)
- Dung so object = tong so thanh vien, dung thu tu, dung `entry_id`.
- `decision in {keep_shared, resolve_distinct, mark_polysemy, uncertain}`.
- `resolve_distinct` -> `chosen_canonical` **in {c.text}** cua entry do; **VA `chosen_canonical != shared_canonical`** (CodeX #2); **VA != canonical cua bat ky member `keep_shared` nao cung nhom** (CodeX #2).
- `keep_shared` -> `chosen_canonical == shared_canonical`.
- `mark_polysemy/uncertain` -> `chosen_canonical == null`.
- **Rang buoc nhom:** khong co 2 `resolve_distinct` trung `chosen_canonical`. Vi pham bat ky rule -> 1 lan re-ask, roi fail nhom do (giu nguyen), log.
- `reason` <= 20 tu.

### 27.5 Ap ket qua (soft-only) + verify
- `resolve_distinct` -> `canonical_target_vi = chosen_canonical`.
- `mark_polysemy` -> `audit_label = polysemy_or_context_dependent`, `injection_action = context_sensitive_translate`. **CodeX #5 (quan trong):** entry polysemy/uncertain **KHONG duoc ro ri canonical cu nhu hard mapping** xuong pack. Cu the:
  - danh dau ro: them `inject_as_hard_canonical = false` (hoac chuyen canonical cu vao field cach ly `canonical_unresolved`, de **khong code nao doc nham** canonical cu la an toan);
  - bo do/injection sim **KHONG** duoc phat `source -> canonical` cung cho cac entry nay (CodeX verify trong sim hien tai);
  - ghi chu: Phase D `context_builder` phai ton trong `context_sensitive_translate` (render mem, dung variant theo ngu canh). Neu chua wire production, **toi thieu artifact C3.5 phai danh dau ro** de khong ai hieu nham canonical cu da an toan.
- `keep_shared` -> giu nguyen.
- Ghi notebook + `decollision_trail.json` (entry: decision/chosen/confidence/reason/provenance-da-chon). **Khong xoa gi.**
- **Verify (CodeX #1 - sua cach phat bieu):** chay lai metric §26. **PHAI bat bien:** gold denominator, matched source terms, **recall A/B**, so entry giu/bo. **KHONG ep bat bien:** VI-agreement/quality - de-collision doi `canonical_target_vi` nen agreement CO THE doi (mong la cai thien) -> **report rieng, khong dung lam pass/fail**. Bat toan bo metric JSON bat bien se fail oan mot fix dung.
- Report: so nhom + bang resolve (truoc/sau canonical), DB hash unchanged, gold-blind=true, cost. Xuat `decollision_trail.json` cho Claude soi (dac biet 2 nhom gradient / product rule).

### 27.6 Scope - noi that (CodeX #6)
- **C3.5 = de-collision pass, KHONG phai full ledger-repair.** No CHI bat cac entry **dung canonical cheo-entry**. Entry co `bad_existing_target` ma **khong dung** voi entry khac -> C3.5 **KHONG** bat (vd se can pass khac). Tren chuong `preliminaries` hien tai cac ca ledger con lai (one/shape/tensor) deu tu lanh (bi bo / da polysemy / ignore lai dung), nen khong co bug ledger co hai bi sot - nhung **dung over-claim** "da sua consolidation phot lo ledger toan he thong".
- **Follow-up rieng (khong nhet vao C3.5):** consolidation rule "single clear `bad_existing_target` proposal -> deterministic apply; multiple competing -> route polysemy/LLM". Lam sau neu Claude duyet.
- KHONG re-run luot Auditor chinh, KHONG bump prompt chinh, KHONG them "translation-quality judge" from-scratch (ca "dich sai am tham khong co + khong dung do" la hiem -> de tang do luong loi ra).

### 27.7 Implementation notes - CodeX (2026-07-01, REVIEW, STOP)

**Implemented:** `pipeline/prepass/builder_v2_decollision.py`, `pipeline/scripts/builder_v2_c35_decollision.py`, `pipeline/tests/test_builder_v2_decollision.py`.

**Commands run:**
- `python -m pytest pipeline\tests\test_builder_v2_decollision.py pipeline\tests\test_builder_v2_audit.py -q --basetemp D:\temp\pytest-builder-v2-c35` -> 9 passed.
- `python pipeline\scripts\builder_v2_c35_decollision.py --estimate-only --out data\reports\builder_v2_c35_decollision` -> 7 groups / 14 members / 1 call / prompt 3629 tok / cap cost $0.01319525 / 0 API.
- `python pipeline\scripts\builder_v2_c35_decollision.py --confirm-usd 0.02 --out data\reports\builder_v2_c35_decollision` -> completed, 1 API call, actual cost $0.00175725, parse_failure=0, DB hash unchanged.
- `python -m pytest pipeline\tests\test_builder_v2_decollision.py pipeline\tests\test_builder_v2_audit.py pipeline\tests\test_d2l_translate_score.py -q --basetemp D:\temp\pytest-builder-v2-c35-final` -> 25 passed.

**Artifacts:** `data/reports/builder_v2_c35_decollision/` contains `collision_groups.json`, `prompts/decollision_001.txt` (full prompt), `raw_outputs.json`, `cost_log.json`, `decollision_trail.json`, `notebook_decollided.json`, `builder_v2_c35_metrics.json`, `builder_v2_c35_decollision_report.json`.

**Result summary:**
- Decisions: `keep_shared=4`, `resolve_distinct=10`, no `mark_polysemy`, no `uncertain`.
- Recall invariants PASS: gold denominator, matched source terms, Metric A recall, Metric B recall, and entry counts unchanged.
- Cost was tiny, but this is still a real paid API run.

**Important quality warning (must review before promotion):**
- De-collision improved the two intended collision classes mechanically, but it also over-split several groups. DEV agreement decreased:
  - Metric A agreement: 0.605263 -> 0.578947.
  - Metric B agreement: 0.647059 -> 0.617647.
- Main suspicious decisions:
  - `backpropagation` changed from shared `lan truyen nguoc` to candidate `truy vet nguoc`, which introduces a new gold disagreement.
  - `derivative` changed from `dao ham` to `vi phan` with low confidence; this is likely too aggressive.
- Therefore `notebook_decollided.json` should be treated as REVIEW artifact, not accepted production memory, until Claude/user reviews `decollision_trail.json`. A safer follow-up may need either (a) apply only ledger-backed `bad_existing_target/canonical_target_change` decisions, or (b) keep target-variant-only splits as proposals instead of automatic canonical changes.

**Potential hole found during implementation:**
- C3.5 fixes cross-entry canonical collisions only. It cannot fix single-entry stale canonical problems unless they collide with another entry. This matches 27.6 but remains a real limitation.
- Some collision members have no alternative candidates, so the model can only `keep_shared`, `mark_polysemy`, or `uncertain`. This is correct under the no-invention rule, but limits repair coverage.

**STOP condition honored:** no commit, no push.

## 28. Stage C3.5 ABLATION: gate (run-1) + prompt v2 pin-owner (run-2) *(Claude, 2026-07-01; rev.2 ap CodeX 3-diem)*

> Muc tieu: do **dong gop tung buoc**. Them gate -> chay lai (run-1) -> so. Roi them prompt v2 -> chay lai (run-2, kem gate) -> so. CodeX implement; byte prompt `d2l_decollision_v2` (28.5) do Claude thiet ke, VERBATIM. STOP khong commit; Claude review + commit.
> Boi canh: run v1 hien tai (xem 27.7) chay dung co che nhung **over-split** (agreement A 0.605->0.579, B 0.647->0.618); moi quyet dinh hai deu **variant-only**, quyet dinh ledger-cung duy nhat (gradient) la dung. Nguyen nhan goc = prompt v1 doi xung, khong co "chu so huu", khong dung tin hieu tan suat.

### 28.0 Bat bien (giu nguyen 27.1)
Mu gold; LLM chi chon trong candidates khong bia; soft-only khong xoa; frozen DB ro; key khong log; STOP khong commit.

### 28.1 Arms + giao thuc so sanh
| arm | prompt | gate | nguon |
|---|---|---|---|
| **baseline** (truoc de-collision) | - | - | A=0.605263, B=0.647059 (SAN, khong duoc tut duoi day) |
| **Arm0** (hien tai) | v1 | KHONG | A=0.578947, B=0.617647 (da co, 27.7) |
| **Run-1** | v1 | CO | can chay |
| **Run-2u** (ungated) | v2 | KHONG | can chay (do RIENG tac dong prompt) |
| **Run-2** | v2 | CO | can chay |

Moi run report:
- **agreement A/B** (vs gold, eval-only) - **CA gated va ungated** voi v2 (vi gate chi ap ledger-backed nen agreement gated cua Run-1 va Run-2 co the bang nhau; tac dong prompt chi lo ra o **Run-2u ungated**).
- **Recall invariants** (gold denominator, matched source, recall A/B, entry counts) - **PHAI khong doi** moi run.
- **Bang per-group before/after** (canonical cu -> moi, decision, applied/held).
- Xuat trail day du cho **Claude review dem tay**: so quyet dinh HAI (doi nham chu) va so quyet dinh DUNG - vi agreement gold **danh gia thap** muc hai (vd `derivative` occ=18 khong nam trong gold subset nen hong ma metric khong thay).

**Ket luan ablation can tra loi:** (1) gate cuu duoc bao nhieu (Run-1 vs Arm0); (2) prompt v2 tu no sua goc bao nhieu (Run-2u vs Arm0, ly tuong ~= baseline = khong regress du KHONG gate); (3) ca hai (Run-2).

### 28.2 RUN-1: gate only (0 API - re-apply trail v1 co san)
Them buoc **gate o apply** (`apply_decollision_to_notebook` hoac pre-filter rows):
- `resolve_distinct` -> **CHI ap (doi `canonical_target_vi`) neu `chosen_canonical` co provenance type in {bad_existing_target, canonical_target_change}** (tra theo candidate objects da co {text, source, type}).
- `resolve_distinct` **variant-only** (provenance = target_variant, hoac type=polysemy_suspected) -> **decision hieu luc = `held_proposal`**: ghi vao trail (giu de review) nhung **KHONG doi canonical**.
- `keep_shared`/`mark_polysemy`/`uncertain` -> giu nguyen logic 27.5.
- **Trail ghi provenance da chon (CodeX #2):** moi row them `chosen_candidate_source`, `chosen_candidate_type`, `applied_status in {applied, held_proposal}`. Gate quyet dinh theo `chosen_candidate_type` truc tiep - **KHONG lookup nguoc text** (tranh mo ho khi cung mot text co o ca target_variant lan conflict_ledger).
- Artifacts: `notebook_decollided_run1.json`, `metrics_run1.json`, trail co cot tren.
- Ky vong: agreement **>= baseline** (revert backpropagation/derivative, giu gradient).

### 28.3 RUN-2: prompt v2 (pin-owner) + gate (API that, nho ~1 call cho 7 nhom)
- Them card fields (28.4), goi `d2l_decollision_v2` (28.5), validate (28.6), roi **ap CUNG gate 28.2**.
- Report **ungated (Run-2u)** truoc khi gate va **gated (Run-2)** sau gate. Artifacts: `notebook_decollided_run2.json`, `metrics_run2.json`, `decollision_trail_v2.json`, prompt luu o prompts/.
- Cache RIENG, khong dung lai cache v1 (prompt khac).

### 28.4 Card them (code, may moc - HINT, khong phai phan quyet)
Moi member them:
- `rejects_shared` (bool) = entry co conflict_ledger entry type in {`bad_existing_target`, `canonical_target_change`} (no tu bao ten dang dung NEN DOI -> **khong duoc lam owner**). *(CodeX #1: nhat quan voi gate 28.2 von coi ca 2 type la bang chung cung.)*
- **Dedup candidate uu tien ledger (CodeX #2):** khi cung mot `text` xuat hien o ca `target_variant` lan `conflict_ledger` -> GIU provenance ledger (manh hon), khong de bi ghi de thanh `target_variant`.
Moi group them:
- `owner_hint` (entry_id) = trong cac member co `rejects_shared=false`, lay member co `occurrences_total` LON NHAT (tie-break: entry_id casefold). Neu TAT CA reject -> `owner_hint=null` (LLM tu quyet).
> Kiem chung tay: gradient(rejects)->non-owner, partial derivative->owner; backpropagation(occ5)>backward(occ1)->owner=backpropagation (giu "lan truyen nguoc", SUA loi v1); derivative(occ18)->owner (giu "dao ham", SUA loi v1); multiplication rule(occ3)>product rule(occ2)->owner. Owner-hint co hoc ra dung chu o moi nhom.

### 28.5 BYTE PROMPT `d2l_decollision_v2` *(Claude thiet ke - CodeX VERBATIM)*
```
[SYSTEM]
You resolve naming COLLISIONS in an English-to-Vietnamese translation memory for the
deep-learning textbook "Dive into Deep Learning" (D2L). Code detected GROUPS: distinct English
source terms that were assigned the SAME Vietnamese canonical. Your job: decide whether a group
is truly ONE concept, or DIFFERENT concepts wrongly sharing a name; and if different, KEEP the
name for its rightful OWNER and give the others a distinct name. You do NOT translate from
scratch and you do NOT invent new Vietnamese wordings - you only choose among the candidates you
are given, or flag.

Work per GROUP, in this protocol:

STEP 1 - same concept or different?
- If the members are the same concept or genuine synonyms (e.g. mean / average; a noun and its
  adjective form), set ALL members to keep_shared. Do NOT split synonyms.
- If you are not clearly convinced they are different concepts, treat them as the same and
  keep_shared. A harmless shared name is better than a wrong split.

STEP 2 - if different concepts, find the OWNER.
- The OWNER is the member that standardly carries shared_canonical. Use owner_hint (a mechanical
  suggestion = the most frequent member that does not reject the shared name) together with the
  evidence. A member whose signals say rejects_shared=true (upstream flagged shared_canonical as
  WRONG for it) is NOT the owner.
- The OWNER keeps the shared name: set it keep_shared. NEVER move the owner off shared_canonical.

STEP 3 - the OTHER members (non-owners).
- For each non-owner, pick from ITS candidates a canonical that differs from shared_canonical and
  from the owner -> decision resolve_distinct.
- If a non-owner has no suitable distinct candidate, or it genuinely has several context-dependent
  renderings, set mark_polysemy (chosen_canonical=null). Do NOT invent a wording.

Hard rules:
- Choose canonicals ONLY from each member's candidates (use "text"). Never invent.
- In a different-concept group, exactly the owner keeps shared_canonical; every resolve_distinct
  must differ from shared_canonical AND from the owner's canonical AND from each other.
- Never drop or delete a term.
- No reference/gold is given; judge from evidence + domain knowledge.
- Recall-safety: prefer keep_shared (unsure about distinctness) or mark_polysemy (unsure about the
  rendering) over a forced guess. If your confidence for a resolve_distinct would be low, use
  mark_polysemy instead.

Reading each member:
- source_term, shared_canonical.
- candidates: the ONLY wordings you may choose from; each has "text" + mechanical provenance
  ("source"/"type"). A conflict_ledger candidate of type "bad_existing_target" or
  "canonical_target_change" is the upstream's OWN correction - a strong hint, but confirm from
  evidence.
- evidence: 1-2 source sentences (use to tell concepts apart).
- signals: occurrences; rejects_shared (whether upstream flagged shared_canonical as wrong for
  this term).
Per group you also get owner_hint: the mechanically suggested owner entry_id (confirm or override
with evidence).

Choose exactly one decision per member: keep_shared | resolve_distinct | mark_polysemy | uncertain.
Set: chosen_canonical (keep_shared -> shared_canonical; resolve_distinct -> a candidate "text"
that differs from shared_canonical; mark_polysemy / uncertain -> null), confidence
(high|medium|low), reason (<= 20 words).

Output: a single JSON array, EXACTLY one object per input member, keyed by entry_id, in the same
order, no commentary:
[{"entry_id":"...","decision":"...","chosen_canonical":"... or null","confidence":"...","reason":"..."}]

Judge only from what you are given. Output nothing except the JSON array.

[USER]
Resolve the following collision groups (each has owner_hint + members). Return the JSON array as
specified.
<GROUPS_JSON>
```

### 28.6 Validator (mo rong tu 27.4)
- Giu nguyen 27.4 (decision hop le; resolve_distinct.chosen in candidates & != shared & != keep_shared sibling & khong trung nhau trong nhom).
- **Them (CodeX #3, lam ro case "khong ai giu shared"; Claude tinh chinh dung-1 -> >=1):**
  - Neu group co BAT KY `resolve_distinct` -> **phai co >=1 `keep_shared`** (owner; chosen==shared). (`>=1` chu khong phai `dung 1`: cho phep cum dong-nghia cung keep_shared + mot member khac resolve, vd element/entry cung "phan tu" + member thu ba doi.)
  - Neu group **khong co `keep_shared` nao** -> **moi member phai la `mark_polysemy` hoac `uncertain`**, KHONG duoc co `resolve_distinct`.
  - => khong bao gio xay ra group bi doi het ten ma khong con ai so huu shared canonical.
- `owner_hint` adherence = **WARNING log, khong fail** (LLM duoc override co ly do).

### 28.7 Khong lam / ky luat
- Gate **co dinh = ledger-backed** xuyen suot Run-1 va Run-2 (de so sanh sach). Tac dong prompt do qua **Run-2u ungated**, khong noi long gate giua 2 run.
- Prompt v2 la **nguyen tac tong quat a-priori** (chu giu ten / conf thap -> polysemy / dong-nghia khong tach). **KHONG duoc van cau chu cho dung 7 nhom nay** (hoc tu). Ket luan "v2 tot hon" chi vung khi do **held-out** o buoc sau - C3.5 nay van la DEV probe.
- product rule -> "quy tac tich" la dung nhung variant-only -> gate van giu proposal; chap nhan (khong regress), xu rieng sau.

### 28.8 Implementation notes *(CodeX, 2026-07-01; status REVIEW)*

Changed code:
- `pipeline/prepass/builder_v2_decollision.py`: add prompt v1/v2 switch, v2 `rejects_shared` + `owner_hint`, candidate provenance preservation, `require_owner` validator, ledger-backed `gate_decollision_rows`, and `applied_status` trail. `apply_decollision_to_notebook` now records the actual prompt version so v2 artifacts are not mislabeled as v1.
- `pipeline/scripts/builder_v2_c35_ablation.py`: new ablation driver. Run-1 replays existing v1 trail with gate (0 API). Run-2 renders prompt v2, estimates cost, then real-runs only under `--confirm-usd`. Full prompts saved under `data/reports/builder_v2_c35_ablation/run2_prompt_v2/prompts/`.
- `pipeline/tests/test_builder_v2_decollision.py`: add guards for v2 owner hint, `rejects_shared`, gate apply/hold behavior, and owner-rule validator.

Commands run:
```powershell
python -m py_compile pipeline\prepass\builder_v2_decollision.py pipeline\scripts\builder_v2_c35_ablation.py pipeline\scripts\builder_v2_c35_decollision.py
python -m pytest pipeline\tests\test_builder_v2_decollision.py -q --basetemp D:\temp\pytest-builder-v2-c35-ablation
python -m pytest pipeline\tests\test_builder_v2_decollision.py pipeline\tests\test_builder_v2_audit.py pipeline\tests\test_d2l_translate_score.py -q --basetemp D:\temp\pytest-builder-v2-c35-ablation-final
python pipeline\scripts\builder_v2_c35_ablation.py --estimate-only --out data\reports\builder_v2_c35_ablation
python pipeline\scripts\builder_v2_c35_ablation.py --confirm-usd 0.02 --out data\reports\builder_v2_c35_ablation
```

Verification:
- Tests: `7 passed`; broader related suite: `28 passed`.
- Frozen DB SHA unchanged: `DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8`.
- Estimate: 1 prompt-v2 call, 3,809 estimated prompt tokens, cap `$0.01324025`.
- Real run: 1 API call via `OPENAI-KEY-2.txt`, 3,491 prompt tokens, 482 completion tokens, cost `$0.00183675`, parse failures `0`, cache hits `0`, cache misses `1`.

Metric comparison:

| arm | prompt | gate | A agreement | B agreement | A recall | B recall |
|---|---|---|---:|---:|---:|---:|
| baseline | - | - | 0.605263 | 0.647059 | 0.666667 | 0.596491 |
| Arm0 | v1 | no | 0.578947 | 0.617647 | 0.666667 | 0.596491 |
| Run-1 | v1 | yes | 0.605263 | 0.647059 | 0.666667 | 0.596491 |
| Run-2u | v2 | no | 0.605263 | 0.647059 | 0.666667 | 0.596491 |
| Run-2 | v2 | yes | 0.605263 | 0.647059 | 0.666667 | 0.596491 |

Interpretation for review:
- Gate alone fully removes the observed Arm0 regression: v1 decisions remain in the trail, but only ledger-hard candidates are applied; variant-only splits become `held_proposal`.
- Prompt v2 is conservative in this run: all 14 members are `keep_shared`. That avoids v1 over-split regressions without needing gate, but also means v2 did not actively apply potentially useful splits such as `gradient -> đạo hàm`.
- Recall invariants are unchanged across arms, as expected; agreement returns to baseline, not above it.
- This remains a DEV probe. Do not promote `run2` output directly into production memory without Claude review/manual harm count, because metric agreement under-covers some non-gold affected occurrences.

Artifacts:
- `data/reports/builder_v2_c35_ablation/ablation_report.json`
- `data/reports/builder_v2_c35_ablation/run1_gate/`
- `data/reports/builder_v2_c35_ablation/run2_prompt_v2/`
- `data/reports/builder_v2_c35_ablation/run2u_prompt_v2_ungated/`
- `data/reports/builder_v2_c35_ablation/run2_prompt_v2_gated/`


### 28.9 CHOT C3.5 *(Claude + user, 2026-07-01)*

**Quyet dinh: LAY GATE, BO prompt v2.**
- **Gate (ledger-grounded auto-apply) = co che chinh thuc cua tang xu ly collision.** Da chung minh: revert sach moi over-split variant-only, chi auto-ap quyet dinh co `chosen_candidate_type in {bad_existing_target, canonical_target_change}`. **Tong quat, khong hardcode term nao** (verify: grep term = rong; logic chay tren ledger-type + occurrences + string-equality).
- **Prompt v2 (pin-owner) BI BO:** ablation cho thay no over-shoot sang **tro** (keep_shared ca 14, khong sua ca gradient). Prompt-tuning dao dong 2 cuc (v1 hung hang -> v2 tro), **khong thang duoc gate**. Ket qua negative trung thuc - giu lai cho luan van (control thuoc ve code-gate, khong phai prompt).

**Guard cuoi (CodeX implement - buoc co hoc cuoi, 0 API) de notebook SACH:**
- Trong `gate_decollision_rows`: sau khi mot `resolve_distinct` duoc xet `applied` (ledger-backed), kiem tra `normalize_target_key(chosen_canonical)` co **trung canonical cua mot entry GIU khac** khong (tao **dung do MOI**). Neu CO -> **khong ap; doi sang `mark_polysemy`** (`audit_label=polysemy_or_context_dependent`, `inject_as_hard_canonical=false`, `canonical_unresolved=<canonical cu>`).
- Day la **luat chung** ("de-collision khong duoc tao dung do moi"), tinh co bat dung `gradient` (->"dao ham" se dung `derivative`/`differentiation` -> chuyen polysemy). KHONG hardcode gradient.
- Chay lai gate 0-API -> `notebook_decollided` sau guard = **memory production C3.5**.

**Do phu (noi that, khong over-claim):**
- Auto-fix: CHI collision co ledger cung. Collision khong-ledger (vd product rule) -> giu `held_proposal`, soi offline. Canonical sai khong-dung-do (gap §27.6) -> ngoai pham vi.
- **DEV-validated tren 1 chuong (preliminaries).** Len sach/chuong moi PHAI re-validate (khong phai sua code - chi dung khang dinh "du cho moi case").

**Trang thai: C3.5 CHOT** cho chuong nay sau khi guard chay xong. Tang collision = gate (tong quat, an toan) + guard no-new-collision.

### 28.10 Guard no-new-collision implementation *(CodeX, 2026-07-01; status REVIEW)*

Implemented the final 0-API guard from §28.9.

Code changes:
- `pipeline/prepass/builder_v2_decollision.py`
  - `gate_decollision_rows(..., notebook=...)` now optionally runs `_guard_no_new_collisions`.
  - Guard computes the effective post-gate canonical map. Any `resolve_distinct` that would create a new canonical collision with another kept entry is converted to `mark_polysemy`.
  - Trail records `blocked_by_no_new_collision`, `blocked_chosen_canonical`, `blocked_candidate_source`, `blocked_candidate_type`, and `blocked_collision_entry_ids`.
- `pipeline/scripts/builder_v2_c35_decollision.py`
  - Applies `gate_decollision_rows(..., gated=True, notebook=notebook)` before writing `notebook_decollided.json`.
- `pipeline/tests/test_builder_v2_decollision.py`
  - Adds regression test that a ledger-backed `gradient -> đạo hàm` proposal is converted to polysemy when `derivative` already owns `đạo hàm`.

Commands run:
```powershell
python -m py_compile pipeline\prepass\builder_v2_decollision.py pipeline\scripts\builder_v2_c35_decollision.py pipeline\scripts\builder_v2_c35_ablation.py
python -m pytest pipeline\tests\test_builder_v2_decollision.py -q --basetemp D:\temp\pytest-builder-v2-c35-guard
python pipeline\scripts\builder_v2_c35_decollision.py --decollision-json data\reports\builder_v2_c35_decollision\decollision_trail.json --out data\reports\builder_v2_c35_decollision
python -m pytest pipeline\tests\test_builder_v2_decollision.py pipeline\tests\test_builder_v2_audit.py pipeline\tests\test_d2l_translate_score.py -q --basetemp D:\temp\pytest-builder-v2-c35-guard-final
```

Verification:
- Tests: target `8 passed`; related suite `29 passed`.
- Re-run status: `applied_existing_decollision_json`; `zero_api=true`; frozen DB hash unchanged.
- Decision counts after guard: `keep_shared=4`, `resolve_distinct=9`, `mark_polysemy=1`.
- `gradient` row:
  - old chosen candidate `đạo hàm` (`conflict_ledger/canonical_target_change`) was blocked.
  - `blocked_collision_entry_ids=["derivative","differentiation"]`.
  - final decision `mark_polysemy`, `applied_status=converted_to_polysemy`.
  - notebook keeps previous `canonical_target_vi="đạo hàm riêng"` but sets `audit_label=polysemy_or_context_dependent`, `injection_action=context_sensitive_translate`, `inject_as_hard_canonical=false`.
- Metric invariants still pass: entry counts, gold terms present, matched source terms, recall A/B unchanged. Agreement remains baseline (`A=0.605263`, `B=0.647059`).

Artifacts refreshed:
- `data/reports/builder_v2_c35_decollision/decollision_trail.json`
- `data/reports/builder_v2_c35_decollision/notebook_decollided.json`
- `data/reports/builder_v2_c35_decollision/builder_v2_c35_metrics.json`
- `data/reports/builder_v2_c35_decollision/builder_v2_c35_decollision_report.json`

Remaining caveat:
- Guard prevents new canonical collisions; it does not decide the best canonical for a polysemous term. `gradient` is now safe/context-sensitive, not "fixed to a better single Vietnamese term." That is intentional for recall-safety.

## 29. Stage C4: WEIGHTED LEDGER PROMOTION (canonical correction / belief revision) *(Claude thiet ke, 2026-07-01; ap CodeX 4-diem review)*

> Muc tieu: sua loi "first-write-wins dong bang canonical sai tu window dau" mot cach **TONG QUAT** (KHONG va rieng gradient). Code XAC DINH, 0-API, blind-gold, chay SAU Auditor. LLM da viet conflict_ledger; code chi **DEM + GATE** (ton trong code-never-does-language-work). Day la analog DON-ENTRY cua C3.5 (von xu collision CHEO).

### 29.0 Bat bien (giu nguyen 27.1 / 28.0)
Mu gold; KHONG bia canonical moi (chi chon tu ledger proposals / source surface); soft (khong xoa entry); frozen DB ro (mode=ro); key khong log; CodeX STOP khong commit; Claude review + commit.

### 29.1 Boi canh (da verify tren notebook C2 + notebook_audited)
- first-write-wins: canonical chot o window dau; window sau CHI ghi conflict_ledger, KHONG doi canonical. Loi gradient sinh o `wb_d2l_preliminaries_008` (canonical "đạo hàm riêng" = SAI khai niem: gradient != partial derivative), bi phan doi o 031/035/036, nhung van bi giu.
- Sua KHONG bang last-write-wins (cung le thuoc thu tu window, cung tuy tien nhu first-write-wins). Sua bang: **trong pass chi ghi ledger -> CUOI pass phan xu XAC DINH tren TOAN BO ledger** (it le thuoc thu tu nhat).
- §29 THAY endpoint C3.5 cho gradient: gradient -> "gradient" (sach, khong collision) thay vi -> polysemy (an toan nhung kem sach).

### 29.2 NGUYEN TAC (general, KHONG hardcode term)
Logic chay tren **SIGNAL** (audit_label, ledger-type, candidate provenance, huong keep-source/translation, collision), KHONG tren chuoi term cu the. 4 entry o 29.6 la **TEST FIXTURE**, moi cai kich 1 gate khac nhau; module promotion **KHONG duoc chua ten term nao** (test grep gradient/tensor/shape/one = rong).

### 29.3 Co che (thu tu gate CO DINH)

**Trigger (candidate set):** entry co conflict_ledger chua >=1 dong type in {`bad_existing_target`, `canonical_target_change`} de xuat target != canonical hien tai (so bang `normalize_target_key`, KEEP dau, NFC+casefold). `polysemy_suspected`/`termhood_suspected` DON LE KHONG kich hard-promote (chi la tin hieu context).

**GATE A — audit-label (termhood). Chay TRUOC moi thu.**
- Chi tiep tuc neu `audit_label in {keep_as_translate_term, preserve_token}`.
- `generic_low_value` / `descriptive_phrase` / `uncertain_low_conf` -> **KHONG promote** (khong sua canonical cua phi-term). [Loai khoi pack = §30 RIENG, khong phai viec cua §29.]
- `polysemy_or_context_dependent` -> da context-sensitive san; **KHONG hard-promote** (giu nguyen Auditor da dat).
> Bat `one` (generic_low_value, du ledger co 6 window -> "một") va `shape` (polysemy) NGAY TAI DAY, truoc khi dem phieu.

**SELECTION (xac dinh, tu ledger) — chi chon HUONG AN TOAN (keep-source):**
- `source surface` = `canonical_source_term` + moi `source_variants[].surface` (normalize cung `normalize_target_key`; NFC+trim+casefold). Trong cac hard-ledger proposal cua entry, NEU co proposal `normalize_target_key(prop)` trung 1 source surface (= keep-source / giu nguyen tu nguon) -> **elect keep-source lam canonical** (safe-fallback).
- NEU KHONG co proposal keep-source (tat ca proposal la BAN DICH moi) -> **KHONG auto-promote**; chi ghi `promotion_status=held_translation_proposal`, giu nguyen canonical + audit/injection hien tai, defer policy/nguoi. KHONG bien mot canonical dang on (vd keep-English style) thanh context-sensitive chi vi co proposal ban dich moi.
> KHONG dem phieu / tie-break giua cac ban dich (gradient von 1-1-1 theo window doc lap, khong co da so). Chi auto khi huong la keep-source. => khong bao gio TU CHON 1 ban dich.

**GATE B — collision (no-new-collision). Sau SELECTION.**
- Neu `normalize_target_key(winner)` da thuoc canonical cua mot entry GIU khac -> **reject** -> `context_sensitive_translate`. (Keep-source it dung do nhung VAN check.)

**Corroboration (anti-hallucination, phu):** keep-source winner phai duoc 1 dong hard-ledger that su de xuat; KHONG tu che keep-source neu ledger khong de xuat no.

### 29.4 Apply
- **auto-promote** (qua het): `canonical_target_vi = winner` (keep-source); ghi `canonical_corrected_from=<old>`; day old vao `target_variants`; `inject_as_hard_canonical=true`.
- **held_translation_proposal** (khong co keep-source winner): KHONG doi `canonical_target_vi`, KHONG doi `audit_label`, KHONG doi `injection_action`, KHONG doi `inject_as_hard_canonical`; chi ghi metadata `promotion_status=held_translation_proposal` + proposal bi giu. Day la case style/policy (vd tensor -> tenxo), khong phai evidence de ha hard canonical hien tai.
- **blocked/context_sensitive** (winner keep-source bi collision, hoac entry da polysemy tu Auditor): KHONG doi `canonical_target_vi` text; `injection_action -> context_sensitive_translate`; `inject_as_hard_canonical=false`; `canonical_unresolved=<old>`; danh `held_for_review`. shape/one giu nguyen nhan Auditor.

### 29.5 Trung thuc / pham vi (loi van theo CodeX 4-diem)
- keep-source auto-promote = **safe fallback canonical**, KHONG phai "dap an tuyet doi". Giu nguyen tieng Anh la mot **LUA CHON STYLE** tinh co it rui ro hon mot ban dich khong kiem chung duoc — chi hop le **SAU** khi qua gate audit + collision. (#1: khong claim "keep-source LUON dung"; mot tu pho thong giu tieng Anh van co the xau, nhung da qua audit-label = term ky thuat nen chap nhan duoc.)
- (#2) gradient -> gradient **ban than cung la style decision**; trong D2L/ML giu "gradient" rat pho bien nen safe, nhung spec goi no la safe fallback chu khong tuyet doi.
- Auto-fix CHI lop HEP: term that + hard-ledger dispute + winner keep-source + khong collision. Con lai -> held metadata hoac context_sensitive neu co collision/polysemy (KHONG pha canonical dang on).
- DEV-validated tren 1 chuong (preliminaries). Sach/chuong moi PHAI re-validate.
- 0-API, deterministic. LLM da viet ledger; code chi dem + gate.

### 29.6 TEST BAT BUOC (chung minh DO PHU + KHONG overfit; moi case 1 gate khac nhau)
| entry | audit_label | ledger winner | ket qua MONG DOI | gate quyet dinh |
|---|---|---|---|---|
| gradient | keep_as_translate_term | gradient (keep-source) | canonical -> "gradient" (AUTO-PROMOTE) | qua het (SELECTION keep-source) |
| tensor | keep_as_translate_term | tenxơ (ban dich) | GIU "tensor"; hard canonical/injection KHONG doi; chi `held_translation_proposal` | SELECTION: khong co proposal keep-source |
| shape | polysemy_or_context_dependent | kích thước | context_sensitive, canonical KHONG -> "kích thước" | GATE A (polysemy) [+ GATE B: size dang giu "kích thước"] |
| one | generic_low_value | một | KHONG promote (canonical giu nguyen trong §29) | GATE A (generic_low_value) |

- Assert them: production promotion module **KHONG chua literal ten term nao** (grep gradient/tensor/shape/one trong production source = rong). Test fixture duoc phep chua cac term nay de chung minh gate.
- Assert them: tong so canonical bi doi tren CHUONG NAY = **dung 1** (gradient). Cac entry khac KHONG bi mutate canonical.
- Moi case ghi ro GATE NAO chan no -> chung minh ca 3 gate deu CAN (bo bat ky gate nao se sai 1 case: bo A -> one bi sua; bo SELECTION/keep-source -> tensor bi Viet hoa; bo B -> shape dung do voi size).

### 29.7 §30 (TBD) — pack-exclusion policy (de SAU, can ban lam ro them)
`generic_low_value` / `descriptive_phrase` + confidence cao -> **loai khoi Translator pack** (wiring THAT vao pack/context builder, KHONG chi report dep), VAN giu trong notebook + audit report. Phan biet ro **"loai khoi pack" != "xoa khoi notebook"**. Day la ly do `one` van xuat hien trong injection_preview o pilot (soft-only). Spec rieng sau.

### 29.8 CodeX implementation notes *(2026-07-01, REVIEW)*
- Implemented `promote_ledger_canonical_candidates()` in `pipeline/prepass/builder_v2_decollision.py`.
- Wired the pass into `pipeline/scripts/builder_v2_c35_decollision.py` before `build_collision_groups()`. New artifacts: `ledger_promotion_trail.json`, `notebook_promoted.json`.
- Real `--estimate-only` run on current preliminaries artifact: `zero_api=true`, `db_hash_unchanged=true`, `canonical_changed_count=1`, status counts = `{promoted_keep_source:1, blocked_audit_label:2, held_translation_proposal:1}`.
- Verified trail: `gradient -> promoted_keep_source`; `one -> blocked_audit_label`; `shape -> blocked_audit_label`; `tensor -> held_translation_proposal` with canonical/injection unchanged.
- Targeted tests: `python -m pytest pipeline/tests/test_builder_v2_decollision.py -q` -> `11 passed`.
- STOP: no commit, no push. Claude should review `ledger_promotion_trail.json`, `notebook_promoted.json`, and targeted tests before commit.

## 30. Stage C4.5: PACK-EXCLUSION POLICY (Translator injection gate + 3-mode renderer) *(Claude thiet ke, 2026-07-01; ap CodeX 4-diem)*

> Muc tieu: quyet dinh entry nao — va RENDER THE NAO — di vao Translator pack, dua tren nhan Auditor. Notebook GIU du 340; pack la tap con. Sua loi pilot soft-only (`one` van hien trong injection_preview vi Auditor chi HA HANG chu khong loai khoi pack). §30 = wiring THAT vao pack/context builder + renderer 3 che do.

### 30.0 Bat bien
- Notebook + audit report GIU DU 340 entry (KHONG xoa). Chi tap PACK (thu that su di vao prompt Translator) bi loc.
- Blind-gold van giu (policy KHONG doc gold). Frozen DB ro (mode=ro). CodeX STOP khong commit; Claude review + commit.
- Chay tren notebook SAU §29 promotion + C3.5 decollision (= ban da sua canonical), khong phai notebook tho.

### 30.1 Gate = `injection_action` (KHONG phai `priority_tier`)
- `injection_action` la coarsening **1:1** cua `audit_label` (verify: moi label -> dung 1 action; generic+descriptive deu -> deprioritize). Day la truong "nhoi vao Translator the nao" => DUNG lam cong loc.
- `priority_tier` CHI de **SAP THU TU** trong tap da-chon (khi co token budget: high truoc, medium sau, cat phan du). TUYET DOI khong dung tier lam dieu kien in/out — mot `context_sensitive` tier=medium VAN phai nam trong pack. (Ở preliminaries low+review==81==tap loai chi la TRUNG HOP, dung dua vao.)

### 30.2 Policy 3 che do (so lieu THAT tren preliminaries, 340 entry)
| injection_action | che do | vao pack? | render |
|---|---|---|---|
| translate (201) | **HARD glossary** | CO | `source -> target`, rang buoc nhat quan |
| preserve (26) | **PRESERVE** | CO | giu nguyen token/API/code |
| context_sensitive_translate (32) | **SOFT hint** | CO, **KHONG cung** | section rieng "context-sensitive, do NOT force" |
| deprioritize (78 = generic 47 + descriptive 31) | **AUTO-EXCLUDE** | KHONG | chi report/UI |
| review_only (3 = uncertain) | **QUARANTINE/REPAIR** | KHONG (mac dinh) | repair-queue artifact, KHONG goi la rac |
- Pack = 227 hard + 32 soft = **259**. Ngoai pack = 78 exclude + 3 quarantine = **81**. (DEV preliminaries; sach/chuong khac se khac — assert theo LABEL, khong hardcode con so.)

### 30.3 Renderer: 3 SECTION TACH BIET (CodeX #3 — load-bearing)
Pack Translator render **3 muc RIENG**, KHONG tron:
1. **MANDATORY TERMINOLOGY** (hard): `translate` (+ `preserve` co the gop nhung phai danh dau do-not-translate). `source -> target`, yeu cau dung nhat quan.
2. **PRESERVE / DO-NOT-TRANSLATE**: `preserve` token (API/code/acronym).
3. **CONTEXT-SENSITIVE TERMS (do NOT force a single translation)**: `context_sensitive`. Render dang canh bao ngu canh, vi du:
   `shape: context-sensitive; "kích thước" for tensor dimensions, keep "shape" for .shape/API, "hình dạng" for abstract shape — do NOT hard-map to one.`
- **CAM**: nhet `context_sensitive` vao muc MANDATORY (pha muc tieu: da nghia ma ep cung). Test PHAI bat cho nay.
- Luu §29: `gradient` da duoc promote sang HARD "gradient" -> da ROI khoi tap context_sensitive; 32 con lai la da-nghia that (vd `shape`).

### 30.4 `review_only` = QUARANTINE, khong phai noise (CodeX #2)
- 3 entry uncertain KHONG vao pack mac dinh, nhung di vao **repair-queue artifact** (vd `pack_repair_queue.json`), danh dau "possible real term, surface unreliable".
- Verify tay 3 cai (2026-07-01): `continuou random variable` (=continuou**s**, term that), `calculu` (=calculu**s**, that), `multiple random variable` (thieu bang chung termhood). **2/3 la term that bi HONG SURFACE** (loi extraction), khong phai rac -> drop im lang se mat term.
- Production end-to-end: co the them 1 pass sua surface sau (`calculu->calculus`, `continuou->continuous`) roi tai xet — KHONG bat buoc nguoi. Truoc mat quarantine artifact la du (khong drop im lang, khong tinh la noise).

### 30.5 Recall accounting (CodeX #4 — DEV, KHONG over-claim)
- Loai 78 `deprioritize` = dung `AUDITOR_DROP_LABELS` (generic+descriptive). Chi phi recall gold DA DO tren preliminaries: **delta Metric A−B = 4 gold false-drop** (A=38/57, B=34/57). **DAY LA KET QUA DEV TREN 1 CHUONG, khong suy rong toan sach.**
- Loai 3 `review_only`: chua nam trong Metric B (khong o AUDITOR_DROP_LABELS). Rui ro recall nho (2/3 term that surface hong) **giam nhe bang quarantine** (khong drop han) -> khong tinh la mat vinh vien.

### 30.6 Wiring THAT (khong chi report) (CodeX #1)
- Cho loc + render 3 section phai nam o **pack/context builder that su nap term vao prompt Translator** — trO `pipeline/translate/prompt.py` va/hoac `pipeline/retrieval/context_builder.py` (CodeX xac nhan diem nap chinh xac), KHONG chi o report/preview.
- Ly do `one` con hien o pilot: Auditor moi ha hang, pack builder van nap ca low-tier. §30 phai loai THAT: entry co `injection_action in {deprioritize, review_only}` KHONG duoc xuat hien trong prompt Translator gui di.

### 30.7 TEST BAT BUOC
- pack CHI chua `injection_action in {translate, preserve, context_sensitive_translate}`; KHONG chua `deprioritize`/`review_only`.
- `one` (generic_low_value/deprioritize) KHONG co trong prompt Translator; mot term `translate` bat ky CO.
- `context_sensitive` term render trong SECTION RIENG, KHONG nam trong MANDATORY glossary block (assert theo section/marker).
- notebook van du 340 (khong xoa); `review_only` xuat hien trong repair-queue artifact.
- Assert theo LABEL/ACTION, KHONG hardcode con so 259/81 (de khong overfit sach khac).

### 30.8 Ngoai pham vi
- KHONG sua Builder extraction (surface hong `calculu` la viec cua pass repair sau, khong phai §30).
- priority_tier-based **budget truncation** (khi pack vuot token budget) la buoc RIENG; §30 chi lam GATE + RENDER, chua lam budget-cut.
- §30 chinh la THAM SO dau vao cua thi nghiem S0-vs-S1 (no quyet dinh cai gi duoc nhoi trong S1).

### 30.9 §5 implementation notes (CodeX, 2026-07-01)
- Implemented pack-exclusion in `pipeline/retrieval/context_builder.py` as an opt-in Builder-v2 notebook path, not a DB migration:
  - `load_notebook_terms()` / `notebook_entries_to_term_rows()` normalize audited notebook entries into term rows.
  - Gate uses `audit.injection_action` only: `translate`, `preserve`, `context_sensitive_translate` enter pack; `deprioritize` and `review_only` do not.
  - `priority_tier` is not used as an in/out gate.
  - `review_only` rows are emitted to `repair_queue` metadata, not prompt text.
- Renderer now has separate sections:
  - `MANDATORY TERMINOLOGY & NAMES` for hard `translate` terms/entities.
  - `PRESERVE / DO-NOT-TRANSLATE` for `preserve`.
  - `CONTEXT-SENSITIVE TERMINOLOGY HINTS` for `context_sensitive_translate`; this section explicitly says not to force the rendering as mandatory.
- `plan_anchors()` now matches notebook entries by all `source_surfaces`, preserving singular/plural/source-variant coverage.
- `pipeline/scripts/run_translate.py` accepts `--memory-notebook` and uses it in both `--preflight-only` and real S1 runs through the same context-builder path. Existing DB-backed runs remain unchanged when the flag is absent.
- Report payload records `memory_notebook.pack_policy_counts` and `repair_queue`.
- Tests added:
  - `test_audited_notebook_pack_exclusion_and_sections` verifies translate/preserve/context-sensitive sections, `deprioritize` exclusion (`one`), and `review_only` quarantine (`calculu`).
  - Updated coverage monkeypatch for the new keyword argument.
- Verification:
  - `python -m pytest pipeline/tests/test_context_builder.py -q` -> 7 passed.
  - `python -m pytest pipeline/tests/test_translate_runner.py -q` -> 11 passed.
  - `python -m pipeline.scripts.run_translate --help` confirms `--memory-notebook` is available.
  - 0-API preflight with `data/reports/builder_v2_c35_decollision/notebook_promoted.json` + preliminaries:
    - policy counts = `notebook_total=340`, `hard_translate=201`, `preserve=26`, `context_sensitive=32`, `report_only=78`, `repair_queue=3`, `pack_total=259`.
    - S1 prompt tokens total est = 55,808; max prompt = 1,886; injected terms avg/max = 18.53/30.
  - Note: `data/reports/builder_v2_c35_decollision/notebook_decollided.json` is still the older pre-§29 endpoint in this checkout (200 hard / 33 context-sensitive). Use `notebook_promoted.json` for §30 verification until the final C3.5 rerun emits a refreshed decollided notebook.

## 31. RE-VALIDATION RUN: full Builder pipeline on a NEW LARGE chapter *(Claude giao CodeX, 2026-07-01)*

> Muc tieu: chay TRON quy trinh C2 -> C3 -> C3.5(+§29) -> §30 tren MOT CHUONG MOI LON (fresh, registry doc lap) de kiem quy trinh co **tong quat** ngoai preliminaries khong. **Day la test CO CHE, KHONG phai test recall-vs-gold** (chuong moi KHONG co gold — eval_glossary_gold chi phu preliminaries 57 term; nen bo qua Metric A/B cho lan nay).

**Chuong: `multilayer_perceptrons`** (641 blocks, 78 windows). Ly do: lon, ky thuat, **chia se gradient/backprop** voi preliminaries -> stress-test §29 tren data moi. (C2 estimate: ~$0.30 nominal / $1.08 cap.)

### 31.0 Guardrails (BAT BUOC)
- **Frozen DB mode=ro, KHONG doi.** Ghi lai sha256 cua `data/jobs/d2l_p1/memory.sqlite3` TRUOC va SAU toan bo run; phai bang nhau (= DA0F...D464B8). Pilot da mo ro (dong 505) nhung van verify.
- **Blind-gold:** Builder + Auditor KHONG doc `eval_glossary_gold` / `reference_eval_only`. (Chuong nay khong co gold nen khong co gi de doc, nhung giu ky luat.)
- **Keys:** env `OPENAI_API_KEY` truoc, roi `OPENAI-KEY-2.txt` (KEY-1 = 429 chet). **KHONG log key.**
- **Out dirs RIENG** (khong de len artifact preliminaries): `data/reports/builder_v2_mlp_c2/`, `..._c3/`, `..._c35/`. (data/reports/builder_v2_* da gitignore.)
- **Cost gate tung buoc:** MOI buoc API chay `--estimate-only` truoc, in cap, roi moi `--confirm-usd <cap+bien>`. **STOP + bao cao sau moi buoc, KHONG chay tran.** KHONG commit (Claude review).

### 31.1 Cac buoc (lenh cu the, chay tu THESIS_RUNTIME_TOOL/)
```
# B0 hash truoc
sha256sum data/jobs/d2l_p1/memory.sqlite3

# B1 C2 Builder (API). Estimate roi confirm.
python pipeline/scripts/builder_v2_pilot.py --chapter multilayer_perceptrons --estimate-only
python pipeline/scripts/builder_v2_pilot.py --chapter multilayer_perceptrons \
    --out data/reports/builder_v2_mlp_c2 --confirm-usd 1.20
# -> data/reports/builder_v2_mlp_c2/notebook.json   [bao: so entry, vai term mau]

# B2 C3 Auditor (API). Estimate roi confirm.
python pipeline/scripts/builder_v2_c3_audit.py --chapter multilayer_perceptrons \
    --notebook data/reports/builder_v2_mlp_c2/notebook.json \
    --out data/reports/builder_v2_mlp_c3 --estimate-only
python pipeline/scripts/builder_v2_c3_audit.py --chapter multilayer_perceptrons \
    --notebook data/reports/builder_v2_mlp_c2/notebook.json \
    --out data/reports/builder_v2_mlp_c3 --confirm-usd <cap>
# -> notebook_audited.json  [bao: phan bo audit_label + injection_action]

# B3 C3.5 decollision + §29 promotion (API nho hoac 0-API). Estimate truoc.
python pipeline/scripts/builder_v2_c35_decollision.py --chapter multilayer_perceptrons \
    --notebook data/reports/builder_v2_mlp_c3/notebook_audited.json \
    --out data/reports/builder_v2_mlp_c35 --estimate-only
#   -> neu muon 0-API: chay lai voi --decollision-json <mark tat ca keep_shared> HOAC confirm-usd nho
# -> ledger_promotion_trail.json, notebook_promoted.json  [bao: canonical_changed_count, cac promoted_keep_source]

# B4 §30 pack preflight (0-API)
python pipeline/scripts/run_translate.py --chapter multilayer_perceptrons \
    --memory-notebook data/reports/builder_v2_mlp_c35/notebook_promoted.json --preflight-only
# -> bao: pack_policy_counts (hard/preserve/soft/report/repair), pack_total

# B5 hash sau (phai == B0)
sha256sum data/jobs/d2l_p1/memory.sqlite3
```

### 31.2 CodeX bao cao (de Claude verify doc lap)
- Cost THUC te tung buoc (nominal, khong log key).
- C2: so entry notebook.
- C3: bang audit_label + injection_action (giong dang §30.2).
- C3.5/§29: `canonical_changed_count`, danh sach `promoted_keep_source` / `held_translation_proposal` / `blocked_*` (dac biet: gradient/backprop xu ly ra sao tren chuong nay?).
- §30: pack_total + 3-mode split.
- **db_hash before == after == DA0F...D464B8.**
- Bat ky bat thuong (window rong, entry hong surface, over-merge cong thuc...).

### 31.3 Claude se kiem lai
Chay lai cac buoc 0-API tren artifact CodeX xuat: dem entry, §29 trail, §30 pack split; doc tay vai entry moi; xac nhan hash frozen. So sanh HANH VI voi preliminaries (co over-extraction giong? §29 co promote sai gi? §30 loc hop ly?) -> ket luan quy trinh co tong quat.

### 31.4 §5 progress notes (CodeX, 2026-07-01)
**B0/B1 complete; STOP before B2 per §31.0 cost-gate.**

Guard findings before API:
- Current checkout DB hash is `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`, not the older expected `DA0F6878...D464B8`. Same hash in both OneDrive and `C:\work` checkouts. DB integrity check passes and counts are `documents=1`, `blocks=8803`, `glossary_entries=1608`, `translation_runs=2604`, `memory_packs=338`.
- Serious guard fix applied before running API:
  - `pipeline/scripts/run_translate.py`: `--preflight-only` now opens DB `mode=ro` instead of `migrate_db()` to avoid mutating frozen DB during B4.
  - `pipeline/scripts/builder_v2_pilot.py`: key search now prefers `OPENAI-KEY-2.txt` before `OPENAI-KEY-1.txt`.
- Targeted tests after guard fix: `python -m pytest pipeline/tests/test_translate_runner.py pipeline/tests/test_builder_v2_pilot.py -q` -> 16 passed.

B1 C2 Builder:
- Estimate: 78 calls, 468,000 prompt tokens, nominal `$0.304200`, cap `$1.075464` (< confirm `$1.20`).
- Real run command: `python -m pipeline.scripts.builder_v2_pilot --chapter multilayer_perceptrons --out data/reports/builder_v2_mlp_c2 --confirm-usd 1.20`
- Result summary:
  - `status=passed`
  - `windows=78`, `applied_windows=78`, `skipped_windows=0`
  - `cache_hits=0`, `cache_misses=78`
  - `parse_failure_count=0`
  - `notebook_entries=546`
  - `rejected_stoplist=6`
  - `conflicts=92`
  - `total_cost_usd=$0.24313575`
- Output artifacts: `data/reports/builder_v2_mlp_c2/notebook.json`, `decision_log.json`, `per_window_audit.json`, `cost_log.json`, `raw_outputs.json`, `prompts/`, `llm_cache.sqlite3`.
- DB hash after B1 remains `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`.

Sample entries from C2 notebook:
- `gradient -> gradient`, occ 30, `status=conflict_pending`
- `backpropagation -> lan truyền ngược`, occ 10
- `multilayer perceptron -> perceptron đa tầng`, occ 15
- `activation function -> hàm kích hoạt`, occ 21
- `hidden layer -> tầng ẩn`, occ 32

Potential issue for Claude review:
- Since the current DB hash no longer matches `DA0F...`, strict §31 hash expectation cannot be satisfied in this checkout. B1 itself preserved the current hash; Claude should decide whether to accept current-hash before/after equality or restore a DA0F snapshot before continuing.

### 31.5 Claude review: frozen-DB hash incident + re-baseline *(2026-07-01)*
- **Su co:** frozen DB hash DA0F...D464B8 -> 64D989...B555C715. Nguyen nhan (CodeX chi ra, Claude xac nhan): `run_translate.py --preflight-only` TRUOC day goi `migrate_db()` (mo WRITE) trong luc lam §30 -> checkpoint/them cot -> doi file bytes.
- **Dieu tra (Claude, verify tren DB):** DATA CON NGUYEN. blocks=8803; `gradient` occ=90 va `updated_at=2026-06-13` (KHONG bi ghi lai row); gold `backpropagation`="lan truyền ngược"; migration 003-006 chi ADD COLUMN (KHONG co INSERT/UPDATE/DELETE). => hash doi la file-level artifact, KHONG phai mutate data. **Moi artifact DA0F cu van hop le.**
- **Quyet dinh: RE-BASELINE frozen hash = `64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715`.** Guard tu day = before==after bang hash nay.
- **Fix da duyet:** (1) run_translate preflight mo `mode=ro` (khong migrate); (2) pilot uu tien OPENAI-KEY-2 truoc KEY-1. Da verify MOI path §31 (pilot/c3_audit/c35/preflight) deu `mode=ro`.
- **Rui ro latent (KHONG thuoc §31):** nhanh run_translate NON-preflight (S1 that) VAN goi migrate_db(write). Xu ly o milestone S1 (output DB rieng hoac ro), khong chan §31.
- **B1 chap nhan:** 546 entries, cost $0.243 (< estimate). `gradient -> gradient` (DUNG NGAY tren chuong nay, khac preliminaries bi "đạo hàm riêng"), `backpropagation -> lan truyền ngược` (khop gold). -> **DUYET chay B2 Auditor.**

### 31.6 CORRECTION: chuong moi CO gold -> DO duoc recall *(Claude, 2026-07-01)*
- Claude noi sai o §31 dau ("khong co gold, bo Metric A/B"). Thuc te `eval_glossary_gold` = 458 term phang (subset_tag='d2l_glossary', KHONG chia theo chuong), va `builder_v2_c3_audit.py` **tu tinh gold subset theo chuong** (`_present_gold_terms(conn, doc_id, chapter_source_text)`, scope = "gold source terms present in chapter source text").
- => B2 chay `--chapter multilayer_perceptrons` se **tu tao Metric A/B** cho MLP. Blind-gold KHONG vi pham (Auditor mu; chi buoc metric doc gold = plane do luong, duoc phep).
- **Yeu cau B2 bao them:** Metric A (registry-vs-gold), Metric B (auditor-filtered), delta = so false-drop, va |gold present in MLP|. Day la bang chung RECALL tren chuong moi -> re-validation tu "chay trơn" thanh "do do chinh xac", manh hon nhieu cho luan van.

### 31.7 Claude review B2 (Auditor tren MLP) — VERIFIED + finding *(2026-07-01)*
- **So khop (verify tay tren notebook_audited + metrics + hash):** 546 entries; keep 344 (63%), preserve 46, polysemy 36, generic 62, descriptive 58. Metric A=68/108=0.62963, B=65/108=0.601852, delta=3. v1 reference=0.6316 (**v2 ~= v1, khong tut recall**). blind_to_gold=True, gold_eval_only=True. DB hash 64D989 giu nguyen. Core terms dung: gradient->gradient (NATIVE-CORRECT, khac preliminaries), backprop->lan truyền ngược, activation/hidden layer/MLP keep.
- **Tong quat: TOT.** recall 0.63 ~ preliminaries 0.667 ~ v1 0.632; keep-ratio + delta tuong duong. Pipeline chay tron tren chuong moi.
- **FINDING (re-validation lo ra):** `model` (occ 84) la **OVER-MERGE bucket** — 24 surface gom ca khai niem KHAC (`neural network`, `network`, `deep neural networks`, `initial matrix`). Auditor thay bucket lon xon -> `generic_low_value` -> `model`->`mô hình` (gold) bi DROP. Goc = **Builder consolidation over-merge head-noun tan suat cao** (cung lop voi `one`->formula-fragments o preliminaries -> TAI LAP).
  - Giam nhe: `neural network` van co entry rieng dung (->`mạng nơ-ron`, keep); bucket ban bi §30 drop nen KHONG inject cu map sai. **Thiet hai rong = 1 gold term (`model`).**
  - Co che dang ngo: `initial matrix`/`neural network` KHONG chua chuoi "model" nhung van bi gom -> merge co the theo evidence-block/embedding, khong chi string. Can dieu tra consolidation (C2) sau — KHONG chan B3.
- **DUYET B3** (C3.5 decollision + §29). Over-merge la van de C2, §29 khong dung (`model` bi GATE A chan vi generic). B3 chay binh thuong.

### 31.8 Claude review B3 (C3.5+§29 on MLP) — VERIFIED + real-collision gap *(2026-07-01)*
- **§29 promotion: canonical_changed_count=0 — DUNG Y DO (khong over-fire).** gradient da la "gradient" san; ledger de xuat "độ dốc" (ban dich) -> §29 HELD (held_translation_proposal), khong doi. Bang chung §29 chi ra tay khi can keep-source, khong pha chuong da dung. backprop->lan truyền ngược giu. DB hash 64D989 nguyen. 546 entries.
- **9 collision groups: 7 la synonym/variant that -> keep_shared DUNG** (validation data/dataset; objective/objective function; generalization/generalize; expectation/expected value; backprop/backward prop; fully-connected hyphen; non-linearity hyphen). LLM tried resolve_distinct voi CUNG canonical -> validator chan dung -> fallback keep_shared. => xac nhan lai bai hoc: LLM decollision hay over-split, guard/validator/fallback deterministic moi giu dung.
- **FINDING THAT (nghiem trong hon CodeX noi): 1 collision THAT bi bo sot va DANG SHIP HARD-INJECT SAI.**
  - `chuẩn hóa` <- [normalization(polysemy), **regularization(keep)**, **standardize(keep)**] = 3 khai niem KHAC nhau. `regularization`->`chuẩn hóa` MAU THUAN gold "điều chuẩn"; `standardize`->`chuẩn hóa` vs gold "chuẩn tắc hóa". Ca 2 la keep_as_translate_term -> **HARD-inject canonical SAI vao S1**.
  - Vi sao khong bat: (a) LLM decollision tra rac -> validator chan; (b) **KHONG co conflict_ledger backing** nen §29 khong dung duoc; (c) keep_shared fallback giu nguyen cum gop. => lo hong: **collision Builder gan cung canonical SAI ma KHONG flag ledger -> khong tang nao bat.**
- **DE XUAT FIX (deterministic, 0 gold, 0 LLM):** khi keep_shared ap cho collision cua cac SOURCE TERM KHAC NHAU (khong phai bien the hinh thai/hyphen), KHONG duoc hard-inject shared canonical cho TAT CA — chi 1 owner giu hard, cac member con lai -> context_sensitive (soft), giong xu ly polysemy cua §30. Tranh ep map sai. (Chua lam — de user quyet.)
- **DUYET B4** (§30 pack preflight, 0-API) — se dinh luong: regularization/standardize xuat hien trong MANDATORY voi canonical sai. B4 chan doan, khong chan.

## 32. Stage C5: TWO DETERMINISTIC GUARDS — over-merge ownership (Fix ①) + collision soft-fallback (Fix ②) *(Claude thiet ke, 2026-07-01; ap CodeX review)*

> Boi canh: re-validation MLP (§31.7, §31.8) lo ra 2 bug C2 THAT + TAI LAP. Fix ① don root over-merge (`model` nuot `neural network`...); Fix ② chan hai collision nhoi-cung-sai (`regularization`->`chuẩn hóa` vs gold "điều chuẩn"). **Ca 2: deterministic, 0 API, 0 gold, KHONG hardcode term** (test grep = rong). CodeX review da siet: dung concept_key (khong exact string), occ=0 quarantine (khong drop mu), collision KHONG chon owner theo occ (soft ca nhom).

### 32.0 Bat bien
- Frozen DB `mode=ro`, hash 64D989...B555C715 khong doi. Blind-gold (fix khong doc gold). Notebook giu du entry (Fix ① detach surface, KHONG xoa entry; Fix ② doi injection_action, khong xoa). CodeX STOP khong commit; Claude review + commit.

### 32.1 Fix ① — SURFACE OWNERSHIP guard (truoc Auditor)
**Van de:** LLM C2 gan surface cua khai niem KHAC vao 1 entry (vd `model` chua `neural network`, `deep neural networks`, `network`, `initial matrix`; nhieu surface `occurrence_count=0`). Auditor thay bucket lon xon -> `generic_low_value` -> mat gold `model`.

**Rule (CodeX #1 — concept_key, KHONG exact string):**
- Voi moi surface_variant `s` cua entry A: neu ton tai entry B (B != A) ma `concept_key(s) == concept_key(B.canonical_source_term)` -> **DETACH `s` khoi A** (no thuoc ve B; B da co entry rieng nen khong mat khai niem). Dung `concept_key` de bat bien the so it/nhieu, hyphen (`deep neural networks` <-> `deep neural network`).
- **occ=0 surfaces (CodeX #2 — quarantine, KHONG drop mu):** surface co `occurrence_count==0` -> chuyen vao artifact `surface_quarantine.json` (nhan "LLM-proposed, never matched"), TACH khoi variant active nhung GHI LAI. KHONG xoa vinh vien (co the la bien the hop le matcher chua bat) -> **do** ty le phantom vs legit sau.

**Vi tri:** buoc consolidation Builder (concept grouping). Chay **TRUOC Auditor** (C2 -> Fix① cleanup 0-API -> C3). De validate tren MLP: ap cleanup len `builder_v2_mlp_c2/notebook.json` (0-API) -> `model` het bi nuot; **re-audit (~$0.08) de xac nhan `model` chuyen sang keep** (tuy chon, CodeX quyet do sau).

### 32.2 Fix ② — CANONICAL COLLISION soft-fallback (o pack build, §30)
**Van de:** >=2 entry `keep`/`translate` khac nhau CUNG `canonical_target_vi` (vd `regularization`, `standardize`, `normalization` -> "chuẩn hóa"; `regularization`->"chuẩn hóa" SAI vs gold "điều chuẩn"), khong ledger, khong resolve -> nhoi CUNG canonical sai vao S1.

**Rule (CodeX — KHONG chon owner theo occ; bao thu):**
- Khi build pack: nhom cac entry `injection_action==translate` theo `normalize_target_key(canonical_target_vi)`. Voi nhom co >=2 entry ma **chua resolve dang tin**:
  - **Ha CA NHOM xuong `context_sensitive_translate` (soft).** TUYET DOI khong chon 1 owner theo occurrence (occ cao nhat o day chinh la `regularization` = mapping sai nhat).
- **Giu HARD chi khi** nhom la bien the CO HOC ro rang: cac member `concept_key` bang nhau, HOAC chi khac hyphen/whitespace/so-it-nhieu; HOAC co ledger/decollision decision hop le gan canonical rieng. (=> `non-linearity/nonlinearity`, `fully connected/fully-connected` van hard; `regularization/standardize` -> soft.)

**Vi tri:** pack/context builder (`context_builder.py`, sau cong injection_action §30). Chi doi cach RENDER/inject, khong sua canonical trong notebook.

### 32.3 PHEP DO tac dong (Claude bo sung — BAT BUOC)
Rule bao thu se ha soft ca mot so synonym DUNG (vd `backpropagation`/`backward propagation`->"lan truyền ngược" neu concept_key khong gop). Chap nhan duoc (soft van duoc nhoi nhu goi y), NHUNG phai DO:
- Bao so entry bi `keep->soft` do Fix ② tren **CA preliminaries LAN MLP**; so surface bi quarantine do Fix ① tren ca 2.
- Neu `keep->soft` LON bat thuong (mem hoa qua nhieu -> hard glossary rong di) -> canh bao, can noi tieu chi "co hoc" cho chinh xac (vd nhan synonym cung token goc) NHUNG CAN THAN khong thanh viec-ngon-ngu.
- Xac nhan **preliminaries KHONG regress** (pack cu 259 khong bi mem hoa sai; 261 test van pass).

### 32.4 TEST BAT BUOC
- **Fix ①:** tren MLP c2 notebook, `model` bi detach `neural network`/`deep neural networks` (concept_key trung entry rieng); surface `occ=0` nam trong `surface_quarantine.json` (KHONG mat); ap len preliminaries KHONG detach nham surface hop le. (Optional: re-audit MLP -> `model` chuyen keep.)
- **Fix ②:** tren MLP decollided, `regularization` + `standardize` KHONG con `translate` "chuẩn hóa" (thanh `context_sensitive`); nhom co-hoc (`non-linearity/nonlinearity`, `fully connected/fully-connected`) VAN `translate`; in so `keep->soft` cho ca 2 chuong.
- **Chung:** module KHONG chua literal ten term (grep model/gradient/regularization/chuẩn hóa/neural = rong); DB hash 64D989 khong doi; preliminaries pack khong regress.

### 32.5 Ngoai pham vi (noi that)
- Fix ② la **damage-control**: no NGAN ep sai, KHONG cho `regularization` ban dich DUNG "điều chuẩn" (can LLM dich lai = phan doan ngon ngu, ma LLM decollision da chung minh khong dang tin). Dung tinh than: guard deterministic chan hai; dich dung la viec LLM va von khong hoan hao.
- Khong dong bo lai toan bo C2 (Fix ① chay nhu pre-Auditor pass tren artifact co san hoac tich hop online sau).

### 32.6 CodeX impl + Claude review (2026-07-02, VERIFIED)

CodeX da implement §32 (chua commit) + chay B4. Claude verify DOC LAP (0-API, tu tai lap so, khong tin bao cao):

**Code (new `pipeline/prepass/builder_v2_guards.py`):**
- Fix (1) `apply_surface_ownership_guard`: owner_by_key = concept_key(headword) -> entry_id (first wins); detach surface khi concept_key(surface)==headword cua entry KHAC; occ==0 -> `surface_quarantine` (khong xoa). Wire TRUOC Auditor trong `run_c3` (guarded_notebook dung cho CA build_term_cards LAN apply_audit -> nhat quan). `load_notebook_entries` chi la json-load thuan -> thay bang guarded["entries"] KHONG mat normalize nao.
- Fix (2) `apply_canonical_collision_soft_fallback_to_rows`: nhom injection_action==translate theo normalize target; nhom >=2 KHONG co-hoc -> ha CA nhom xuong context_sensitive_translate; co-hoc (concept_key bang HOAC shape-key [0-9a-z] bang) -> giu hard. Chi sua field audit, KHONG dong canonical. Wire tai `notebook_entries_to_term_rows` (pack build that).

**So Claude tu tai lap (khop CodeX):**
- DB hash = 64D989...B555C715 (frozen, khong doi). grep term-cung (model|gradient|regulari|chuan|neural|network|standardize|backprop) trong guard module = RONG.
- Surface guard: MLP-c2 detached=58 quarantined=72; PRELIM-c2 detached=19 quarantined=38. Vi du detach dung ngu nghia: `axis 0`<-axis, `column dimension`<-column, `deep learning models`<-deep learning, `differential calculus`<-calculus (dung pattern over-merge head-noun §31.7).
- Pack collision fallback: MLP(notebook_decollided) hard=336 soft=44 preserve=46 report=120 pack_total=426; softened 4 nhom [backpropagation, generalization, regularization+standardize, validation data], giu hard co-hoc [fully-connected, non-linearity]. PRELIM(notebook_decollided) hard=196 soft=37 total=259 (khop §30 cu 259); softened 2 nhom [conditional independence, multiplication/product rule]. (Luu y: notebook_promoted ra 197/36 — chenh 1 vs decollided 196/37; artifact dung la notebook_decollided.)
- Tests: `test_builder_v2_guards.py` (Fix1: detach plural qua concept_key + quarantine occ=0 + giu model/models) + `test_context_builder.py` (Fix2: assert tren RENDER that — regularization/standardize NGOAI mandatory + TRONG context-sensitive; non-linearity/fully-connected TRONG mandatory). Full pipeline sweep = **263 passed** (baseline 261 -> +2, khong vo cho khac).

**B4 (§30 pack preflight MLP, artifact `data/reports/builder_v2_mlp_b4_pack_preflight.json`):** 0-API/0-DB-write; pack_total=426 (hard 336 / preserve 46 / soft 44), report_only 120, repair 0; S1 prompt tokens min/avg/max 898/1434/1951, total 86054, upper 331814; injected/window avg 24.82. Noi dung khop repro Claude. Sensitive placement dung: regularization/standardize -> soft (het hard mandatory), `regularization constant`->`hang so chuan hoa` van hard (entry khac, hop ly), fully-connected/non-linearity van hard.

**Ket luan:** §32 dat muc tieu. Fix (1) don over-merge don goc (pre-Auditor); Fix (2) chan collision nhoi-cung-sai (`regularization`->`chuan hoa`) o pack. Deterministic, 0 gold, khong hardcode. Tradeoff bao thu (softening backpropagation/backward-propagation dung synonym) da disclose + do duoc, chap nhan (soft van vao pack).

**Follow-up (khong chan):** (a) CLI `run_translate --preflight-only` return 0 ngay sau `_print_preflight` (dong 131) -> KHONG tu ghi artifact JSON; B4 hien dung tay bang `_preflight()`. Nen them nhanh ghi report cho preflight de artifact-hoa tu dong. (b) Muon xac nhan `model` chuyen dropped->keep can re-audit API tren notebook_surface_guarded (chua chay, ~$0.08). (c) latent risk run_translate non-preflight (real S1) van migrate_db(write) — xu ly o moc S0-vs-S1.

Commit: (dien sau).

### 32.7 Re-audit MLP — Fix (1) end-to-end CONFIRMED (Claude ran API, 2026-07-02)

Chay lai C3 Auditor tren MLP QUA guard (`--confirm-usd 0.40`, KEY-2, out `builder_v2_mlp_c3_reaudit/` = gitignored). Cost THUC $0.0825; 28 calls; **DB hash 64D989 khong doi**; auditor blind_to_gold=True.

**Ket qua chinh — `model` DUOC CUU (dropped -> kept):**
| | audit_label | injection_action | occ | variants |
|---|---|---|---|---|
| TRUOC (xo ban, §31.7) | generic_low_value | deprioritize = BO khoi pack | 84 | 24 |
| SAU (da guard) | polysemy_or_context_dependent | context_sensitive_translate = VAO pack (soft) | 68 | 10 |

Nhan qua truc tiep cua Fix (1): xo sach 24->10 variant -> Auditor doc ra `model` la khai niem context-dependent that (khong con la rac generic) -> khong drop nua. **`model` khong con nam trong false-drop.**

**Nhung noi that (khong to hong):**
- `model` ve dang **SOFT (context_sensitive)**, KHONG phai hard glossary. Hop ly (model von generic/da nghia), va no DA vao pack = tinh la recovered cho recall. Nhung khong bat buoc.
- **Recall tong gan nhu PHANG:** Metric A=0.6296 (68/108), Metric B=0.6111 (66/108), delta=2/108. So voi pre-guard (~0.630) khong tang dang ke — DUNG ky vong: Fix (1) la fix DIEM (chan 1 over-merge false-drop), khong phai nang recall dien rong.
- **false-drop 3->2 NHUNG khong phai subset sach:** con lai {`category` occ5, `data` occ30}, ca hai generic_low_value. `data` (occ30) gio bi drop — la lan audit MOI, stochastic. `parse_failure_count=2` (2/28 chunk khong parse duoc) = nhieu run-to-run. Nen so sanh THANH PHAN false-drop giua 2 lan la co nhieu; claim SACH duy nhat = `model` cu the da lat dropped->kept.
- Fix (1) KHONG don sach 100% xo `model`: `network`(occ1), `initial matrix`(occ1) van con vi khong co entry doi thu de detach (can phan doan ngon ngu = viec Auditor). Auditor du van xu ly OK (gan polysemy).

**Ket luan:** Fix (1) chay end-to-end dung muc tieu — cuu duoc gold term bi over-merge lam mat, 0 hai DB, blind-gold. Fix diem, khong phai don bay recall. Guard tu day nam SAN trong run_c3 (0-API, truoc Auditor) nen chuong moi khong ton them lan goi nao; $0.0825 nay chi la audit bu 1 lan cho MLP (da audit truoc §32).

Artifacts reaudit gitignored (regenerable). Task-file note commit-only.

## 33. Tiered gold-miss diagnostic — recall 0.63 khong phai "Builder mu 37%" *(Claude lam truc tiep, 2026-07-02)*

> Cau hoi cua user: recall 0.63 la tot hay xau? 546->426 term co phai Builder do / bo loc thieu / gold khong phu? Tra loi bang DU LIEU + them 1 lop chan doan CO HOC, **KHONG doi thuoc chinh** (recall strict giu nguyen lam headline — khong duoc sua thuoc de diem dep).

### 33.1 Gold provenance (kiem tu DB, mode=ro)
`eval_glossary_gold` = 458 dong tu `glossary.md` cua du an d2l-vn, dong bang o commit c775d6b4998e..., nhieu term co `discussion_url` (github git.io) — tuc **NGUOI lam, co tranh luan cong dong truoc khi chot**. Ban chat: SO TAY VAN PHONG cho ca cuon sach (~23 term/chuong) de nhieu dich gia nguoi thong nhat — ke ca tu thuong (fit/key/value/switch). Khac muc dich voi Builder (memory day dac theo chuong) -> "426 vs 108" khong so truc tiep duoc; va Translator moi window chi nhan ~25 term (B4: min/avg/max 8/24.8/44), KHONG an 426 cung luc.

### 33.2 Co che (impl trong `builder_v2_metrics.py::_tier_gold_misses`, wire vao `recall_vs_gold_dev.missing_tiered_metric_a`)
Phan tang CO HOC (khong phan doan ngon ngu, khong hardcode):
- **phrase_covered**: chuoi token cua gold term xuat hien NGUYEN-TOKEN trong 1 surface DAI HON cua registry (word-boundary: `perceptron` trong `multilayer perceptron` = covered; `fit` trong `overfit` = KHONG — day la ly do so nay dang tin hon substring tho).
- **absent**: khong co dau vet token nao trong registry.
- Nhan "tu thuong vs ky thuat" tren nhom absent = chu thich TAY cua nguoi (code-never-does-language-work).
Test: `test_builder_v2_metrics.py` (perceptron/vector covered; fit/batch-size absent). Full sweep **264 passed**.

### 33.3 So chinh thuc (tren artifact da co, 0-API)
| Chuong | gold present | recall strict (headline) | phrase_covered | ABSENT | recall kem phu-cum (chan doan) |
|---|---|---|---|---|---|
| MLP | 108 | **0.630** | 22 | **18** (16.7%) | 0.833 |
| Preliminaries | 57 | **0.667** | 3 | **16** (28.1%) | 0.719 |

- MLP absent 18 = {analogy, batch size, causality, computer vision, constrain, dimension, dimensionality, fit, implement, module, normalize, orthogonal, pattern recognition, quadratic, recall, scalar, supervised learning, switch}. Chu thich TAY: ~4 cum ky thuat that (batch size, computer vision, pattern recognition, supervised learning — deu la term nhac-qua, khong phai loi cua chuong); con lai la tu don kieu so-tay-van-phong.
- Preliminaries absent 16 = {agent, category, coefficient, end-to-end, fit, implement, implementation, key, layer, metric, population, prior, query, recall, scale, switch} — pattern khac MLP: it phu-cum hon (3), absent nhieu tu don generic hon. Tiering KHONG phai lam dep dong deu — no lo ra prelim yeu hon MLP o phan absent, do la thong tin that.

### 33.4 Cach dung khi bao cao (de khong tu lua)
Noi voi GVHD: "Recall exact-match vs gold style-guide = 0.63; phan tang co hoc cho thay 20% la lech do hat (khai niem co o muc cum), sot tuyet doi 17% (MLP), trong do cum ky thuat that ~4%. Gold la thuoc TUONG DOI cho S0-vs-S1 (cung mau so hai tay), khong phai chan ly tuyet doi; metric dinh cua luan van la CONSISTENCY khong can gold." KHONG bao gio thay 0.833 vao vi tri cua 0.63.

## 34. C6: Translator hardening + UI cascade layer + SMOKE TRIAL S0/S1 tren MLP *(Claude thiet ke sau khi review Translator + scorer, 2026-07-02 — GIAO CODEX)*

> Boi canh: Claude da audit Translator (prompt/profiles/context_builder/run_translate) va scorer cascade (§ rieng: T1 bge-m3 / T2 code / T3 LLM locate-only). Ket luan: thiet ke DU de chay thu S0-vs-S1, NHUNG phai vao 4 viec sau truoc. Trong luot nay CodeX lam A (0-API) + B (0-API) + C (smoke trial, CO API, cost-gate). Da FIX truoc do boi Claude: stale hash DA0F->64D989 trong cascade_localize + ambiguous_assignment (commit babf7a9).

### 34.0 Bat bien (nhu moi luot)
- **Frozen DB `data/jobs/d2l_p1/memory.sqlite3` TUYET DOI khong mo write.** Hash 64D989...B555C715 truoc==sau moi buoc. DB nay gitignored — khong co backup git, ky luat ro la lop bao ve DUY NHAT.
- Blind-gold cho moi thanh phan chay truoc scoring. Keys: env -> OPENAI-KEY-2.txt (KEY-1 chet 429). KHONG log key.
- Moi buoc API: estimate/preflight -> BAO CAO -> cho confirm. STOP sau moi phase. KHONG commit (Claude review + commit).
- Artifact moi vao data/reports/ (gitignored neu regenerable). Test: khong hardcode term.

### 34.1 [A1 — 0-API] WORKDB guard: run that KHONG duoc dung frozen DB
**Van de:** `translate_windows` ghi `translation_runs` + `memory_packs` vao CHINH conn duoc truyen; nhanh non-preflight cua run_translate con goi `migrate_db()` (write). Chay that tren frozen DB = doi hash + tron output vao nguon.
**Yeu cau:**
- Them flag `--workdb PATH` cho run_translate. Khi chay non-preflight: BAT BUOC co --workdb; script tu copy frozen DB -> PATH neu chua ton tai (giu nguyen neu da co, in canh bao resume); mo WORKDB read-write (migrate_db tren workdb OK), frozen path sau khi copy KHONG duoc dung nua trong run.
- Neu user lo chay non-preflight ma khong co --workdb VA --db tro vao data/jobs/d2l_p1/ -> **RAISE ngay** (thong bao ro "frozen DB is read-only; pass --workdb").
- Report ghi: frozen_db_sha256 (truoc/sau, phai bang nhau), workdb_path, workdb_sha256_after.

### 34.2 [A2 — 0-API] Budget 500->1500 + CAU CHI NO TO
**Boi canh do that (Claude, 105 window / 2 chuong):** trần 500 hien KHONG cat gi (max pack 384 MLP / 239 prelim), so term/window do ANCHORING quyet dinh. Nhung window nang nhat da 77% tran, va hanh vi khi vuot la CAT LANG LE (`dropped_by_budget` ghi vao pack nhung run van tiep) — trai triet ly "tran la cau chi chong bug, khong phai diet".
**Yeu cau:**
- Nang default `--context-budget` 500 -> **1500** (run_translate arg) VA `translate_windows(context_budget_tokens=...)` default (runner.py) — ca 2 cho, tranh lech.
- **Fuse:** trong ca preflight LAN run that, neu BAT KY window nao co `pack.dropped_by_budget` khong rong -> **RAISE** (liet ke window_id + term bi rot). Khong duoc cat lang le. (Preflight hien co _raise_if_preflight_unsafe — noi them check nay; run that check trong translate_windows truoc khi goi LLM cho window do.)
- Test: fixture ep budget nho -> raise dung thong bao; budget du -> chay binh thuong.

### 34.3 [A3 — 0-API] preflight --report phai ghi file
`run_translate --preflight-only --report X` hien in console roi `return 0` (dong ~131) khong ghi file (phat hien o B4). Sua: preflight-only + --report -> ghi JSON preflight (dung noi dung da in) roi return. Test.

### 34.4 [B — 0-API] UI overlay: them LOP MARK tu cascade (display-only)
**Boi canh:** overlay (app/backend/services/thesis_overlay.py) mark VI bang allocate_spans (surface, cung lib voi scorer — tot), status tu d2l_translation_metrics_v2.json. NHUNG app KHONG doc quyet dinh cascade -> occurrence dich LECH chuan (T3 dinh vi duoc "dich gia thuc su viet gi") khong co mark VI: EN co mark, VI trong — sai muc tieu "mark 1:1".
**Yeu cau:**
- Overlay nhan them (optional) duong dan cascade report JSON (output run_cascade_localize / T3). Voi moi decision co target_start/target_end (t2_credit) hoac target_quote da validate (T3 found): phat mark VI lop thu 2, phan biet ro: `mark_source: "surface_form"` (hien tai) vs `"cascade_t2"` vs `"cascade_t3_llm"`.
- **Display-only:** khong doi metric, khong doi score report. Khong co cascade report -> overlay giu nguyen hanh vi cu (khong loi).
- T3 quote -> span: dung logic dinh vi trung lap voi `_locate_quote_span_in_region` (import tu cascade_localize, KHONG copy-paste).
- Test backend: fixture cascade report nho -> overlay tra mark 2 lop dung vi tri; khong co report -> nhu cu.

### 34.5 [C — CO API, cost-gate] SMOKE TRIAL S0 + S1 tren chuong Builder moi (MLP)
**Muc tieu:** chay thu DAU-CUOI tren mau nho de kiem day chuyen (khong phai thi nghiem chinh; khong ket luan metric tu mau nay).
**Thiet ke:**
- Them flag `--max-windows N` cho run_translate (cat danh sach windows SAU build_windows, theo thu tu; ghi vao report `windows_truncated_to: N`).
- Chay: chapter `multilayer_perceptrons`, **--max-windows 10**, ca S0 va S1; S1 voi `--memory-notebook data/reports/builder_v2_mlp_c35/notebook_decollided.json`; `--context-budget 1500`; `--workdb data/jobs/trial_s0s1_mlp/memory.sqlite3`; out/report `data/reports/trial_s0s1_mlp/`.
- **Trinh tu bat buoc:** (1) preflight-only + report -> BAO CAO cost cap -> CHO USER CONFIRM; (2) chay S0; (3) chay S1; (4) bao cao. Uoc luong tham khao: ~10 window x 2 config, prompt ~1.2-1.5k/window -> cap du kien duoi $1; so THAT lay tu preflight.
**Acceptance checks (CodeX tu kiem + bao cao, Claude se verify lai):**
1. Frozen DB hash truoc==sau==64D989 (workdb rieng).
2. Prompt S0 (log/memory_packs) KHONG chua section terminology nao (S0 purity).
3. Prompt S1 chua 3 section §30 (MANDATORY / PRESERVE / CONTEXT-SENSITIVE) voi term dung anchoring.
4. **§32 song trong prompt that:** `regularization`/`standardize` KHONG nam trong MANDATORY cua bat ky window nao; neu xuat hien thi phai o CONTEXT-SENSITIVE. `fully connected layer`/`non-linearity` neu xuat hien phai o MANDATORY.
5. Moi block co ban dich (JSON contract du key); parse failure/retry bao cao ro.
6. Khong window nao co dropped_by_budget (fuse 34.2 im lang).
7. Cost log day du (nominal + cap), khong log key.
**Bao cao them de nguoi doc mat:** in 3 cap (block EN / S0 VI / S1 VI) co cung 1 term memory-sensitive de user doc doi chieu nhanh.

### 34.6 Thu tu + STOP
A1 -> A2 -> A3 -> B (0-API, tests xanh het, bao cao) -> **STOP cho confirm** -> C preflight -> **STOP cho confirm cost** -> C run -> bao cao cuoi. KHONG commit.

### 34.7 GHI SO (KHONG lam trong luot nay)
- **Builder prompt v9 candidate (tu review §33.x cua Claude):** them 1 cau vao updates_to_existing: "chi danh cho bien the cua CUNG khai niem; khai niem khac du chua headword phai vao new_terms" — ung vien sua goc over-merge (§32 Fix (1) dang chan ha nguon roi; chi lam khi co ke hoach re-run + do recall).
- **Phrase-covered la chu dich thiet ke:** prompt v8 bao emit cum chinh xac thay vi tu don -> 22/40 miss la phu-cum (§33). Dua vao chuong phuong phap luan van nhu mot quyet dinh granularity co van ban + co do, khong phai bug.
- S0-vs-S1 THI NGHIEM CHINH: thiet ke a-priori o § tiep theo sau khi smoke trial sach.

### 34.8 CodeX review — 7 siet, Claude CHAP NHAN het (2026-07-02, verify tren code that)

CodeX duyet §34 + de 7 siet. Claude kiem tung diem tren file that truoc khi ghi nhan — **chap nhan ca 7**, trong do:

1. **--workdb**: resolve absolute; REFUSE neu workdb==db hoac workdb nam trong data/jobs/d2l_p1/; resume phai in "resume existing workdb". **Smoke trial BAT BUOC workdb SACH + experiment_id MOI** — Claude verify: runner co resume-skip that (`resume_all_blocks_present`, runner.py ~121-130) -> workdb cu se lang le tra ban dich cu nhu moi ("resume lam dich gia"). Acceptance them: report `windows_skipped` PHAI = 0 trong smoke.
2. **Budget default o 3 CHO khong phai 2** — Claude verify: `build_context_pack(budget_tokens: int = 500)` (context_builder.py:296) la cho thu 3 THAT. Sua ca 3 -> 1500. Ly do doi default lib: budget la cau chi khong phai diet, default rong hon an toan cho MOI caller/profile.
3. **Fuse truoc API call trong real run**: HOI TU — spec §34.2 da dat guard trong translate_windows truoc khi goi LLM; CodeX xac nhan anchor dung la cho context_pack co san truoc `_call_with_reask`. Giu nguyen.
4. **Overlay chi emit mark khi span CHAC**: t2_credit co target_start/end, hoac T3 found + quote validate + dinh vi duoc; failed/not_rendered/ambiguous -> BO QUA, khong doan. (Nhat quan ky luat bao thu cua ca he.)
5. **Acceptance #4 phai check LINE-LEVEL + source-term EXACT, khong substring** — bat chuan: `regularization constant -> hang so chuan hoa` la entry KHAC, hop le nam MANDATORY, contains-tho se false alarm (Claude da gap dung ca nay o B4). Test parse tung dong "- <source> -> <target>", so source EXACT (casefold), khong substring.
6. **--max-windows ghi vao CA preflight report LAN translation report**: tong windows goc, sau truncate, danh sach window_id — tranh nham smoke 10-window voi full chapter khi doc report sau nay.
7. **Preflight report them field audit**: frozen_db_sha256_before/after, workdb_path, memory_notebook, context_budget, max_windows, llm_config, zero_api=true.

Thu tu giu nguyen §34.6: A1->A2->A3->B (0-API + tests, bao cao) -> STOP -> C preflight -> STOP cost -> C run -> bao cao. KHONG commit.

### 34.9 Claude review STOP-1 — A1/A2/A3 + B PASS, cho phep C preflight (2026-07-02)

Claude verify DOC LAP (khong tin bao cao):
- **A1 workdb** (doc diff): non-preflight bat buoc --workdb (SystemExit neu thieu); refuse workdb==db VA workdb nam trong thu muc frozen; copy2 khi chua co, WARNING resume khi da co; preflight van mo frozen mode=ro; report ghi frozen sha before/after + workdb sha. Dung ca 7 siet §34.8 lien quan.
- **A2 budget+fuse**: 1500 o CA 3 CHO (grep "500 default" = CLEAN); fuse `_raise_if_context_dropped_by_budget` dat SAU build pack, TRUOC build_messages/API; test `test_runner_raises_before_api_when_context_drops_by_budget` assert **client.calls == []** — dung assertion cot tu (API chua bi goi khi fuse no).
- **A3**: preflight-only + --report ghi file that (test_preflight_only_writes_report_and_truncation_metadata), kem truncation metadata (--max-windows).
- **B overlay cascade**: import `_locate_quote_span_in_region` tu cascade_localize (REUSE khong copy); merge display-only voi mark_source surface_form/cascade_t2/cascade_t3_llm; khong co report -> "not_requested" hanh vi cu; report hong -> "unavailable:*" khong crash; decision thieu manh -> skip khong doan.
- **Tests Claude tu chay**: pipeline **268 passed** (263 -> +5), app backend **134 passed**. Frozen DB hash 64D989 nguyen.
- Khoang ho nho (non-blocking, ghi nhan): chua co test rieng cho refuse workdb-nam-trong-thu-muc-frozen (co test workdb==db cung ho); overlay T3 bo qua report thieu span (dung nguyen tac, CodeX da khai).

**KET LUAN: STOP-1 PASS. Cho phep CodeX chay C preflight** (0-API): chapter multilayer_perceptrons, --max-windows 10, S0+S1, notebook_decollided, budget 1500, workdb SACH `data/jobs/trial_s0s1_mlp/memory.sqlite3` + experiment_id MOI (vd `trial_s0s1_mlp_v1`), --report ra data/reports/trial_s0s1_mlp/preflight.json. Bao cost cap -> STOP cho user confirm truoc khi goi API.

### 34.10 SMOKE TRIAL S0/S1 PASS + workdb-inheritance bug fixed (2026-07-02, Claude verified)

**Su co giua chung (CodeX phat hien + tu sua, Claude xac nhan root cause TREN CODE):** workdb copy tu frozen DB mang theo translation_runs/memory_packs cu (P3). runner.py co `SELECT pack_id FROM memory_packs WHERE pack_id=? -> if existing: return` ma pack_id KHONG chua experiment_id -> workdb "moi" se GIU PACK CU va bo qua ghi pack moi. Fix: `_purge_runtime_state` xoa 4 bang runtime (evaluation_runs, qa_issues, translation_runs, memory_packs) CHI tren nhanh copy-moi (khong dung resume), co guard ton-tai-bang. Claude them unit test purge (runtime sach + bang static giu nguyen; bang thieu khong loi). Da xoa workdb ban va rerun sach.

**Ket qua smoke (Claude tu doc workdb, khong nhan so qua loi ke):**
- frozen 64D989 nguyen; workdb E4D8DF00... ; translation_runs = 80 S0 + 80 S1 DUNG experiment trial_s0s1_mlp_v1, khong con row la; memory_packs 10+10; windows_skipped=0; JSON fail 0; dropped_by_budget 0.
- **S0 purity: 0/10 pack S0 chua section terminology.**
- **§32 SONG trong prompt API that (line-level exact):** khong dong MANDATORY nao la regularization/standardize; window 001 co `regularization -> chuẩn hóa (context-sensitive; do not force)`; nonlinearity/non-linearity nam MANDATORY. -> Chuoi Builder->Auditor->§29/30/32->pack->prompt xac nhan DAU-CUOI lan dau tien.
- Cost that: S1 fresh 10 calls $0.018927, S0 cache-hit (thua huong replay cache P3 cung prompt+model); rerun sach $0. Duoi cap $0.17.
- **Glitch output "либо" (ky tu Nga):** REAL — S0 blocks mlp_b006 + mlp_b049, VA CA S1 block mlp_b049 (CodeX bao thieu ve S1). La artifact model gpt-5.4-mini code-switching, xuat hien CA HAI arm -> khong lien quan glossary. GHI SO cho thi nghiem chinh: them output-hygiene check deterministic (flag ky tu Cyrillic/CJK trong ban dich VI) vao acceptance — re, 0-API, bat duoc loi model.

Tests sau fix + test moi: 21 targeted + 4 run_translate script = xanh. Commit ca reports trial (28K, la record smoke).

## 35. THI NGHIEM CHINH S0-vs-S1 FULL-CHAPTER — thiet ke A-PRIORI (khoa truoc khi nhin ket qua) *(Claude, 2026-07-02 — GIAO CODEX)*

> Smoke trial §34.10 PASS -> day la phat sung chinh cua luan van: do memory (S1) co lam ban dich NHAT QUAN + BAM CANON hon S0 khong, tren ca chuong that. Thiet ke nay KHOA TRUOC: chuong, arm, metric headline, policy, hygiene. Sau khi nhin ket qua KHONG duoc chinh knob roi chay lai tren cung benchmark (bai hoc dont-tune-intervention-on-test).

### 35.0 Bat bien
- Frozen DB mode=ro, hash 64D989 truoc==sau moi buoc. Workdb rieng, experiment_id MOI. Blind-gold cho translate; metric step DUOC doc gold. Keys env->KEY-2. Cost gate: preflight -> STOP bao cap -> confirm -> chay. CodeX STOP theo phase, KHONG commit.
- **INPUT DONG BANG (pin theo commit hien tai):** notebook MLP = `data/reports/builder_v2_mlp_c35/notebook_decollided.json`; notebook prelim = `data/reports/builder_v2_c35_decollision/notebook_decollided.json`; §30/§32 pack policy nhu da commit; budget 1500; prompt s0_d2l_v1/s1_d2l_v1; model gpt-5.4-mini temp/seed nhu llm_translate.yaml. Nhin ket qua xong muon doi BAT KY thu gi trong danh sach nay = thi nghiem MOI (v2), khong ghi de.

### 35.1 [D1 — 0-API + reask] OUTPUT-HYGIENE layer (lam TRUOC khi chay)
Phat hien smoke: code-switching Cyrillic (`либо` = "either") 3/160 block, CA HAI arm (S0 b006+b049, S1 b049) — loi output cua LLM da ngu, khong phai loi pipeline.
- **DETECT (code, co hoc):** voi moi block output, tinh tap Unicode-script cua OUTPUT tru tap script cua SOURCE block; giao voi {Cyrillic, CJK, Hangul, Thai} khac rong -> flag. (Khong cam Greek — cong thuc toan dung σ/α/β; luat tru-source tu mien nhiem ten rieng nuoc ngoai.) KHONG phan doan ngon ngu trong code.
- **REASK (LLM, toi da 1 lan/block-window):** dich lai window kem note tinh "previous output contained non-Vietnamese-script characters; retranslate" (note doi cache-key -> temp=0 khong tra lai ban loi cu). Phan doan sua thuoc LLM.
- **Con dinh sau reask -> ghi `qa_issues` + dem vao report theo arm.** Khong loop.
- Report field moi: `hygiene: {flagged_blocks, reasked, fixed, still_bad, by_config}`. Test: fixture output co Cyrillic -> flag+reask; source co san Cyrillic -> KHONG flag; Greek trong toan -> KHONG flag.

### 35.2 Arms & scope
- **2 arm:** S0 (khong memory, purity nhu §34) vs S1 (pack tu notebook_decollided qua §30+§32). Khac nhau DUY NHAT hard-constraint block (da co code-doc "S0 PURITY").
- **2 chuong, 2 invocation, CUNG experiment_id `exp_s0s1_builderv2_v1`, CUNG workdb `data/jobs/exp_s0s1_full/memory.sqlite3` (sach, purge tu dong):**
  1) multilayer_perceptrons (60 windows, pack 426) — notebook MLP.
  2) preliminaries (45 windows, pack 259) — notebook prelim.
- Ly do 2 chuong: tranh ket luan 1-chuong; prelim con cho phep doi chieu voi cac artifact §30 cu.
- Cost du kien: S0 phan lon cache-hit tu P3 (smoke da thay); S1 fresh ~105 window — ngoai suy smoke ($0.019/10w) ~ $0.20 nominal; cap bao thu < $1.5. SO THAT lay tu preflight, STOP bao truoc khi chay.

### 35.3 METRIC HEADLINE (khoa truoc)
1. **B — occurrence-weighted registry adherence** (pipeline hien co, ban JOINT-allocation da sua; scope cham == scope dich {heading,prose}). Bao theo chuong va gop.
2. **D — consistency** (cung term -> cung rendering giua cac block; D_surface_v1 nhu da relabel). Bao S0 vs S1.
3. **Hygiene counts** (35.1) theo arm — truc chat luong phu, KHONG phai headline.
4. **Gold agreement** (vs eval_glossary_gold, denominator = gold-present-in-chapter; §33 tiering DI KEM lam chu thich, KHONG thay headline).
- Cascade T1+T2 (0 LLM) chay nhu DIAGNOSTIC neu embed endpoint san sang; T3 GPT van prompt-review-gated, quyet dinh RIENG sau khi xem residual. EV-02 judge (Gemini pairwise) = quyet dinh RIENG sau khi co ban dich — khong thuoc scope §35.
- **Du doan dang ky truoc (de khong tu lua):** ky vong S1 > S0 ro ret o B (smoke preview MANDATORY-subset: 0.974 vs 0.833 — metric khac, chi la prior); S1 >= S0 o D; hygiene tuong duong 2 arm (~2% block, loi model khong lien quan memory). Neu S1 KHONG thang B -> nghi van day chuyen inject, dieu tra truoc khi tin.

### 35.4 Trinh tu + STOP (CodeX)
1. Implement 35.1 (detector+reask+tests) -> chay full test suite -> **STOP-1 bao cao** (Claude review).
2. Preflight ca 2 chuong (0-API, --report) -> **STOP-2 bao cost cap** -> user confirm.
3. Chay MLP S0 -> S1; roi prelim S0 -> S1 (workdb sach 1 lan dau, cac invocation sau resume CUNG experiment la dung y do — bao ro windows_skipped tung lan).
4. Bao cao chay: acceptance nhu §34.10 (purity 0 pack S0; §32 line-level exact tren pack S1; dropped_by_budget=0; hash frozen nguyen; JSON fail; hygiene report; 3 cap doc mat moi chuong; cost that). **STOP-3.**
5. Claude verify doc lap tren workdb -> roi moi chay scoring 0-API (B/D/gold + cascade diagnostic neu co endpoint) -> §35.x ket qua.

### 35.5 Acceptance bo sung so voi smoke
- windows_skipped: lan dau moi chuong = 0; neu re-invoke resume thi giai trinh ro rang tung con so.
- Hygiene layer hoat dong: co it nhat log flag/reask neu glitch xuat hien (smoke bao truoc ~2%); khong reask lap vo han.
- Report per-chapter TACH BACH (khong tron 2 chuong vao 1 so).


<!-- S35_STOP1_REVIEW -->
## 35.6 — STOP-1 review (Claude, 2026-07-02): D1 hygiene layer VERIFIED, APPROVED

Independent verification (not trusting the report):
- `pipeline/translate/hygiene.py` read line-by-line: mechanical script-diff only (unicodedata.name prefix -> {Cyrillic, CJK incl. kana, Hangul, Thai}); Greek/math and Latin never flagged; source-subtraction key check passed (runner query aliases `text AS clean_text, original_text AS source_text`, detector falls back correctly).
- Reask flow: max 1 hygiene reask, note is static + mechanical (no term/gold leakage, S0/S1 symmetric), appended assistant+user turns change the cache key. Still-bad -> translation IS persisted + `qa_issues` row (`hygiene_foreign_script:<script>`, tier1/major, retry_count=1). Verified `run_id` in scope at persist site.
- Usage accounting fix confirmed: WindowRunReport now sums ALL attempts (calls/tokens/cost), `from_cache = all(...)`; this also retro-fixes the old JSON-reask undercount.
- Tests: 3 new hygiene tests assert DB state not just return values. Ran full suites myself: pipeline 272 passed, backend 134 passed. Frozen DB hash unchanged 64D989...B555C715.

Known edge case (accepted, not a blocker): if the hygiene reask itself returns invalid JSON, the window ends `failed` and the attempt-0 valid-but-dirty translation is not persisted. Visible in the report, window re-runnable; a fallback-to-dirty would need new code in a locked experiment — defer unless it actually fires.

Caveat carried from CodeX (agreed): flagged-script set is exactly the §35 spec set; extending (Arabic/Hebrew/Devanagari) = separate task, not mid-experiment.

Next: STOP-2 — preflight both chapters (0-API), report cost cap, wait for user confirm.


<!-- S35_STOP3_MLP_REVIEW -->
## 35.7 — STOP-3 review, MLP pair (Claude, 2026-07-02): VERIFIED on workdb, ACCEPTED

Scope note: user narrowed this run to the MLP chapter only (cost caution, decided BEFORE seeing results — allowed under 35.0). Preliminaries S0/S1 remain committed as part of exp_s0s1_builderv2_v1, to run after this review.

Independently verified on data/jobs/exp_s0s1_full/memory.sqlite3 (ro) + data/jobs/translate_cache.sqlite3 (ro):
- translation_runs: S0 475/60w + S1 475/60w, single experiment_id, 0 failed/skipped; frozen DB hash recomputed 64D989... unchanged.
- S0 purity: all 60 S0 packs anchors_count.terms == 0, no constraint content. S1 packs terms min/avg/max = 8/24.8/44 (matches B4 preflight).
- §32 verified on the NEWEST cached request for w_001 (2026-07-02 09:11): three sections MANDATORY / PRESERVE / CONTEXT-SENSITIVE HINTS present; `regularization -> chuẩn hóa (context-sensitive; do not force)` in HINTS, absent from MANDATORY. Pitfall logged: cache holds a STALE 2026-06-14 pre-§32 entry for the same tag where regularization WAS mandatory (điều chuẩn) — any future prompt audit must select by created_at, not tag alone.
- Cache-hit anatomy (explains S0 60/63 hits): a full pre-§32 MLP run existed in translate_cache from 2026-06-14. S0 prompts were untouched by §32 (S1-only change), so all 60 S0 windows replayed free; 3 of those cached S0 responses contained Cyrillic (либо) and D1 correctly flagged+reasked them fresh (3/3 fixed) — D1 works on cached content too. S1: only the 10 post-§32 smoke windows hit; 2 hygiene reasks; still_bad 0 both arms.
- Hygiene ground truth: re-ran detect_hygiene_issues over all 950 persisted outputs -> 0 foreign-script issues; qa_issues table 0 rows. Report hygiene fields match (S0 3/3/3/0, S1 2/2/2/0).
- Cost: incremental S0 $0.00721 + S1 $0.10433 = $0.11154, far under cap.

Observation for scoring (NOT a knob to touch now): S1 soft hint proposes "chuẩn hóa" for regularization (collision-era canonical; gold says "điều chuẩn"), and sampled S1 output follows the hint while S0 chose "chính quy hóa". §32 soft fallback prevented a mandatory error but the hint still steers — this is the [[memory-injection-precision-cost]] pattern; measure in scoring, tune only in a future experiment id.

Next: prelim S0/S1 run (same workdb/experiment), then 0-API scoring per 35.3.


<!-- S35_MLP_SCORING -->
## 35.8 — MLP scoring results (Claude, 0-API, 2026-07-02): pre-registered predictions checked

Erratum first (honesty ledger): in the STOP-2 discussion I claimed "P3 never translated MLP" — WRONG. P3 (2026-06-14) translated all 4 chapters incl. MLP 475+475; my GROUP BY probe only showed the first 15 alphabetical groups (all intro). The user's instinct ("S0 dịch rồi đâu cần dịch lại") was correct; in practice S0 replayed from P3 cache for $0.007, so no waste occurred. Lesson: never eyeball a LIMITed GROUP BY.

Command: score_run --db workdb exp_s0s1_full --experiment exp_s0s1_builderv2_v1 --chapters d2l_multilayer_perceptrons --gold-variants data/eval/d2l_gold_variants.csv. Stage gates all pass; denominator identical both arms (1182 gold source occ; scope==translation scope).

Headline (locked in 35.3):
- B gold occurrence adherence (flat): S0 0.7580 -> S1 0.7657 (+0.008). Prediction "S1 > S0" HOLDS, margin small.
- D registry consistency: S0 0.7590 -> S1 0.8253 (+0.066, 166 terms). Thesis-headline metric (needs no gold) shows the real effect.
- Hygiene: 0 foreign-script in all 950 outputs (§35.7).

Why B moved so little — conditional decomposition (diagnostic, run on locked tooling, no knob changed). Gold terms split by whether the injected pack canonical is among gold accepted forms:
- pack==gold (39 terms, 549 occ): S0 0.860 -> S1 0.965. Injection works (+10.5pt).
- pack!=gold (20 terms, 116 occ): S0 0.440 -> S1 0.026. S1 obeys its pack ~97% — scored as "wrong" by gold because Builder is BLIND to gold by design (mày học vs học máy, lô nhỏ vs minibatch, quy tắc chuỗi vs quy tắc dây chuyền, tập xác thực vs tập kiểm định, đơn vị ẩn vs nút ẩn...). These are valid-Vietnamese style divergences, not mistranslations.
- not in pack (399 terms, 517 occ): 0.721 vs 0.720 — no injection, no effect. Clean internal control; also confirms the mechanism has zero side-effect outside its pack.

Interpretation locked to 35.3 discipline: aggregate strict B stays the reported headline; the decomposition is a diagnostic footnote. Mechanism compliance ~0.96-0.97 in BOTH directions (matches smoke prior 0.974). The 20 disagree-canonicals are a canonical-QUALITY question (future arm: gold-informed canonical review or T4 human pass under a NEW experiment id), not a mid-experiment fix. A_registry (vs old 1608-row glossary_entries) is reported but ruler-misaligned with the actual notebook pack — treat as legacy diagnostic only.

Artifacts: data/reports/exp_s0s1_builderv2_v1/metrics_mlp.json (+occurrence audit csv/html). Next per 35.4: prelim S0/S1 run, then same scoring; D2L UI overlay can now point at the workdb runs.


<!-- S35_PRELIM_TASK -->
## 35.9 — TASK for CodeX: run the preliminaries S0/S1 pair (completes §35.2 scope)

User confirmed. This is execution of the already-locked design — NO knob changes, NO new flags, NO prompt edits.

Do:
1. Run d2l_preliminaries S0 then S1, exactly as preflighted (preflight_preliminaries.json):
   - experiment_id exp_s0s1_builderv2_v1
   - workdb data/jobs/exp_s0s1_full/memory.sqlite3 — ALREADY EXISTS with the MLP runs. Do NOT recreate/purge it; the resume path must warn-and-continue. Prelim pack_ids (pk_*_w_d2l_preliminaries_*) are new, so no stale-pack reuse is possible; state this check in the report.
   - notebook: (as used in preflight_preliminaries.json)
   - context budget 1500, --report data/reports/exp_s0s1_builderv2_v1/run_preliminaries.json
2. Expected cost: S0 mostly replays from P3 cache (P3 covered preliminaries on 2026-06-14); S1 fresh except 0 smoke windows. Nominal ~$0.10-0.15, cap $0.762 (preflight). Abort and STOP if incremental cost passes $0.40 mid-run.
3. STOP after runs (no commit). Report: per-arm windows/blocks/failed/skipped, calls & cache hits & incremental cost, hygiene stats, frozen-DB hash before/after (must stay 64D989...), workdb hash after, 2-3 sample S0-vs-S1 term renderings.

Do NOT score. Claude will verify on the workdb and run the 0-API scorer (same command as §35.8, chapters d2l_preliminaries), so measurement stays with the reviewer.

Pre-registered expectations (logged before results, per 35.3): D_consistency S1 > S0 (MLP prior +0.066); B small positive move with the same pack-vs-gold dilution pattern; hygiene still_bad 0; ~0 windows skipped. If S1 does NOT beat S0 on D, that is a red flag for the prelim pack — report, do not "fix".


<!-- S35_CASCADE_MLP_TASK -->
<!-- S35_CASCADE_MLP_CODEX_STOP_A -->

### 35.10 §5 — CodeX implementation notes / STOP-A (2026-07-02)

Implementation:
- Added thin experiment-scoped CLI `pipeline/scripts/run_experiment_cascade.py`; no prompt/cascade/overlay logic was changed.
- CLI opens the experiment workdb with SQLite `mode=ro`, uses frozen DB only for hash guard, and writes only JSON reports + embedding cache/T3 cache.
- Preflight path runs T1 bge-m3 through local LM Studio, T2 code rules, and estimates T3 locate-only calls/cost using locked `d2l_locate_only_v4` prompt + `pipeline/configs/llm_adjudicator.yaml`. It does **not** call GPT.
- Run path is implemented but guarded by `--confirm-usd`; not executed in STOP-A because the estimate exceeds the §35.10 >500-call stop rule.
- Added tests in `pipeline/tests/test_experiment_cascade.py`; targeted suite: `13 passed`.

STOP-A command:
`python -m pipeline.scripts.run_experiment_cascade --preflight-only --db data\jobs\exp_s0s1_full\memory.sqlite3 --frozen-db data\jobs\d2l_p1\memory.sqlite3 --experiment exp_s0s1_builderv2_v1 --chapter multilayer_perceptrons --configs S0,S1 --embed-endpoint http://127.0.0.1:1234/v1/embeddings --models bge-m3=text-embedding-bge-m3@gpustack/bge-m3-GGUF:Q8_0 --cache-dir data\eval\embed_cache\cascade_exp_mlp --out-dir data\reports\exp_s0s1_builderv2_v1 --llm-config pipeline\configs\llm_adjudicator.yaml --llm-cache data\reports\exp_s0s1_builderv2_v1\cascade_t3_cache.sqlite3`

Artifact:
- `data/reports/exp_s0s1_builderv2_v1/cascade_mlp_preflight.json`

STOP-A results:
- Embedding endpoint: available (`bge-m3` / `text-embedding-bge-m3`).
- Workdb unchanged: true. Frozen hash before/after: `64d98965f8859869...` / `64d98965f8859869...`.
- S0 denominator 2885; T2 resolved 1763 (61.1%); T3 residual estimate 1122 (38.9%).
- S1 denominator 2885; T2 resolved 1751 (60.7%); T3 residual estimate 1134 (39.3%).
- T3 total estimate: **2256 calls**, prompt tokens ~1,499,770, output cap 1,155,072, cost cap **$2.6851**.
- Hard stop triggered: `t3_estimate_over_500_calls=true`. No T3/GPT call was made.

Notes / caveats:
- The high residual count is not an API failure; it comes from running the occurrence cascade across the full MLP registry occurrence set (2885 occurrences per arm). The expensive subset is all T2-residual occurrence assignments (C0/multiple/collision/shared-variant), not a tiny sample.
- Because §35.10 explicitly says `>500` estimated T3 calls must stop for review, CodeX did not produce `cascade_mlp_S0.json` / `cascade_mlp_S1.json` with T3 marks yet. A narrower T3 policy or explicit approval is needed before STOP-B.

### 35.10 §5.1 — STOP-A rerun with corrected notebook+gold scope (2026-07-02)

Change:
- Default cascade term scope changed from legacy frozen registry to `notebook_plus_gold`.
- Notebook terms are loaded from `data/reports/builder_v2_mlp_c35/notebook_decollided.json` through `notebook_entries_to_term_rows`, filtered to `injection_action in {translate, preserve, context_sensitive_translate}`.
- Source variants are expanded into source surfaces. Gold targets are loaded from `eval_glossary_gold` plus `data/eval/d2l_gold_variants.csv`. Terms are deduped by normalized source surface with accepted-form union.
- Existing T1/T2/T3 logic and prompt remain unchanged.

Rerun command: same as STOP-A, with explicit `--notebook data\reports\builder_v2_mlp_c35\notebook_decollided.json --gold-variants data\eval\d2l_gold_variants.csv --term-scope notebook_plus_gold`.

Artifact:
- `data/reports/exp_s0s1_builderv2_v1/cascade_mlp_preflight.json` (overwritten with corrected-scope preflight)

Corrected-scope results:
- Term scope: notebook 546 entries -> 546 rows; notebook source surfaces considered 656; gold sources considered 458; terms after source dedupe 992.
- S0 denominator 2487; T2 resolved 1167 (46.9%); T3 residual estimate 1320 (53.1%).
- S1 denominator 2487; T2 resolved 1334 (53.6%); T3 residual estimate 1153 (46.4%).
- T3 total estimate: **2473 calls**, prompt tokens ~1,637,888, output cap 1,266,176, cost cap **$2.9418**.
- Hashes still unchanged; bge-m3 endpoint still available; no GPT call was made.

Before/after vs legacy-registry preflight:
- Legacy registry: denominator 2885/arm, T3 calls 2256 total, cap $2.6851.
- Notebook+gold: denominator 2487/arm, T3 calls 2473 total, cap $2.9418.
- Interpretation: scope correction removed old-registry-only occurrences, but did **not** shrink the T3 workload. The remaining large residual is dominated by notebook+gold terms whose accepted forms are not uniquely present in the aligned T1 region (C0/multiple/collision/shared-variant). This is now a real cost/coverage question for the experiment scope, not just a stale-registry artifact.

STOP state:
- `t3_estimate_over_500_calls=true` remains. CodeX again stopped before T3. No `cascade_mlp_S0.json` / `cascade_mlp_S1.json` with T3 marks has been produced.

## 35.10 — TASK for CodeX: full measurement cascade (T1→T2→T3) on MLP exp runs, for UI 1:1 marks

User request: run the WHOLE localization cascade on exp_s0s1_builderv2_v1 MLP (both arms), so the overlay can mark EN↔VI terms 1:1 including the LLM tier. §35.9 (prelim run) stays queued behind this.

Ground rules (unchanged):
- This is MEASUREMENT/DISPLAY — read-only on workdb data/jobs/exp_s0s1_full/memory.sqlite3; zero writes to any DB except T3 api-cache file; frozen hash must stay 64D989... B/D scores in §35.8 are final for this experiment — the cascade adds localization info, it must NOT recompute or alter scoring.
- Scope per §34-B layering: primary marks come from surface_form (allocate_spans, 0-API, already available). The cascade runs on the RESIDUAL occurrences that surface matching could not credit/mark — tiered: T1 bge-m3 region narrowing (LOCAL endpoint http://127.0.0.1:1234/v1/embeddings, $0; preflight_embedding_model must pass, else STOP and report unavailable), T2 code rules ($0), T3 LLM locate-only ONLY for what T2 can't cleanly resolve (prompt d2l_locate_only_v4 UNCHANGED, temp 0, validate quote-in-region, prefer found=false; api-cache on; OPENAI key discipline as always).
- Existing CLI (localizer_cascade --run-dev) is gold-CSV/DEV-scoped. Add a minimal experiment-scoped entry (new flag or thin script) that: pulls scope blocks + translations for exp_s0s1_builderv2_v1 / d2l_multilayer_perceptrons / config in {S0,S1}, builds the residual occurrence set, runs the cascade, and emits per-config reports data/reports/exp_s0s1_builderv2_v1/cascade_mlp_S0.json and cascade_mlp_S1.json in the EXACT schema app/backend/services/thesis_overlay._merge_cascade_marks consumes (mark_source cascade_t2 / cascade_t3_llm, uncertain decisions skipped). No changes to cascade internals, prompts, or overlay logic.

Sequence with 2 STOPs:
1. STOP-A preflight (0 paid API): residual occurrence counts per config, predicted tier split, T3 call estimate + cost cap (report BEFORE any T3 call), embedding endpoint identity check result, list of any code you had to add + tests for it.
2. After user cost confirm: run S0 then S1. STOP-B: tier shares (%surface-resolved / %T2 / %T3-found / %unresolved), T3 validation-rejection count, 5 sample marks per arm (EN occurrence -> VI quote), cost actual vs cap, frozen hash, confirmation of zero workdb writes. No commit.

Claude then verifies STOP-B on artifacts (re-run validate on quotes, spot-check marks against workdb text, overlay smoke with cascade_report pointed at the new files).

Pre-registered expectations: tier profile near EV-D2L-10 (T2 resolves most of residual; T3 small; locate-acc prior 99.1%); S0 and S1 residual sizes differ (S1 followed pack forms, surface layer should already mark more of S1's gold/pack terms; S0 free renderings lean harder on T2/T3). If T3 volume estimate is large (>500 calls), STOP-A must flag it — do not assume approval.


<!-- S35_LOCAL_T3_CALIB -->
## 35.11 — TASK for CodeX: calibrate LOCAL models as T3 locator (same 103-item gold as the 98.1% GPT baseline)

Goal: decide whether a local LM Studio model can replace gpt-5.4-mini for the §35.10 DISPLAY-layer T3 run (and future full-book cascade passes, ~$10+/pass at API prices). Decision by measurement, not vibes.

Test set & fairness (do NOT improvise):
- Items: exactly the 103 T3 residual records embedded in data/reports/cascade_gold_locate_measure.json (t3_records) — the same set where gpt-5.4-mini scored 101/103 = 0.9806. Same prompt builder d2l_locate_only_v4, same validate_payload, same correctness criterion as that measure script. Reuse its code path; only the LLM client endpoint changes.
- Baseline: GPT numbers come from the existing cache/report — 0 paid API calls in this whole task.

Candidates (all via LM Studio OpenAI-compatible endpoint http://127.0.0.1:1234/v1):
1. openai/gpt-oss-20b — reasoning effort LOW (user measured ~40 tok/s)
2. qwen/qwen3.5-9b — thinking OFF (~48 tok/s)
3. google/gemma-4-12b — thinking OFF (~15 tok/s; include for accuracy, flag speed)

ONE shared decoding profile, set PER-REQUEST via API params (overrides UI sliders; document in report):
- temperature 0, top_p 1.0, top_k disabled, min_p disabled, seed fixed 20260612
- repeat_penalty OFF (=1.0) — MANDATORY: the task is verbatim copying; repeat penalty actively corrupts quotes. If a model's server ignores the override, report it.
- max_tokens 512; response_format json_schema if LM Studio accepts it for that model, else json_object (validator is the real gate either way)
- reasoning/thinking = off/low as listed; do NOT test higher effort (see reasoning-effort lesson: it eats output budget and buys nothing for extraction)
- Load-side: context 8192 is enough (~800-token prompts); note GPU offload config used.

Report per model: locate-acc /103 vs gold, validator-rejection count, JSON-parse failures, median latency & tok/s, projected wall-clock for the §35.10 workload (1,724 calls plan A). Plus 3 sample failures with the wrong quote shown.

Acceptance to adopt for display-T3: acc >= 0.95 (>=98/103) AND rejections+parse-fails < 5%. If several pass, pick the fastest. gpt-5.4-mini stays as fallback tier for items the winner fails validation on (cost-ascending cascade, same philosophy as T1->T2->T3).

STOP after the comparison table. No commit. Frozen DB untouched (this task reads only the report JSON + local endpoints).


<!-- S35_LOCAL_T3_CALIB_B -->
### 35.11b — Amendments after CodeX review (accepted 1,2,3,4,5; rejected early-stop)

- Status of this test: DEV/calibration decision tool for the DISPLAY layer only — never cite as a thesis-final metric.
- Report per model MUST split: correct / valid_but_wrong (validator passed, span != gold) / not_found_wrong / quote_validation_fail / json_parse_fail (separate, to expose think-tag or prose pollution).
- Production plan when a local model is adopted: GPT fallback for validator-reject AND confidence=low (kept even at pass-tot); plus stratified audit of 30-50 of the 1,724 production calls against workdb text.
- Param safety: log the exact request params sent AND the model id echoed in responses; ALSO set repeat_penalty=1.0 in the LM Studio load/UI config as second belt (API may silently ignore overrides). Load context 4096 (not 8192): prompts ~800 tok + 512 output; smaller KV reservation frees VRAM for more GPU layers.
- Decision thresholds (CodeX proposal accepted): >=101/103 & reject<5% -> local as primary; 98-100 -> local + GPT fallback + sample audit; <98 or flaky JSON -> GPT plan A.
- Rejected: prioritized order with early stop. 103 items = 5-10 min/model; run ALL THREE for the full comparison table (thesis methodology material).


<!-- S35_LOCAL_T3_DECISION -->
## 35.12 — Calibration verdict (Claude verified on artifacts, 2026-07-03): Gemma 4 12B adopted as local T3 primary

Verified independently from data/reports/local_t3_locator_calibration.json: per-item recount 103/103 correct (criterion locate_contains_gold, same as GPT baseline scoring); the two items GPT 5.4-mini missed (function@b047 S0+S1) Gemma answered gold-exact ("hàm"); model_echo uniform google/gemma-4-12b; request profile as locked (temp 0, repeat_penalty 1.0, seed 20260612, json_schema, ctx 4096).

Results: Gemma 103/103 (beats GPT 101/103) | Qwen3.5-9B 97/103 (real not_found_wrong misses) | GLM-4.7-flash 28/30 probe | GPT-OSS-20B 0/103 (emits schema-placeholder JSON "S0..??"/"..." — unusable for locate). Isolated 30-case rerun after CodeX's unload-guard fix confirms Gemma 30/30, median 3.62s/call.

Caveats on record: (1) Gemma returned confidence=high on all 103 — its confidence channel is non-informative; production safety = validator + GPT fallback on validator-reject + 40-call stratified audit (confidence=low tier will simply never fire). (2) Full-103 file's gptoss qvf=206 reflects the pre-fix double-count; isolated rerun files are the clean speed reference. (3) DEV/display-layer decision only, never a thesis-final metric (35.11b).

Consequence for §35.10: T3 cost drops to $0 -> scope decision reverts to FULL residual (2,473 calls, ~2h30m local), NOT the 1,724 UI-blind trim. GPT 5.4-mini = fallback tier for validator-rejected items only.


<!-- S35_T3_SPEED_PROBE -->
### 35.12b — Concurrency & GPT speed probe (verified): Gemma conc-3 confirmed for production

CodeX probe (verified on artifacts): Gemma concurrency plateau at 3 (30 cases: seq 113.9s -> c3 102.7s -> c4 no gain), 30/30 at every level. Fresh GPT API probe c3: 30/30, 14.4s, $0.00768/30 (ran without prior confirm — de minimis, but paid probes must ping first next time).

Rescaled to the REAL §35.10 scope (2,473 calls, not 1,724): Gemma ~2h21m/$0 vs GPT ~20m/~$0.63. DECISION: Gemma stays primary (equal-or-better quality 103/103, $0, offline-reproducible; speed only matters for interactive iteration, this is run-once-cache-forever). Production run = Gemma, concurrency 3. GPT = validator-reject fallback tier unchanged.


<!-- S36_CANONICAL_REELECTION -->
## 36 — DESIGN (v2, do NOT implement during exp_s0s1_builderv2_v1): blind canonical re-election + no-gold harm detection

Status: DESIGN LOCKED by user + Claude discussion 2026-07-03. Implementation queued AFTER the current experiment completes (prelim pair + cascade + overlay). Nothing here touches the locked experiment.

### 36.1 Forensic evidence (from notebook_decollided.json, MLP)
The two genuinely-harmful canonicals found by the §35.8 gold-decomposition were both SELECTION failures, not knowledge failures — the correct answer was already inside each entry:
- `population` -> canonical "quần thể"; target_variants already contained "tổng thể" (correct stats term) from the FIRST evidence block. Auditor saw it exactly (label polysemy_or_context_dependent, reason "Statistical population vs ordinary people/domain") -> its label soft-gated the term. Auditor was neither blind nor powerless; it lacks (by design) the right to re-elect canonicals.
- `regularization` -> canonical "chuẩn hóa"; target_variants already contained "điều chuẩn" from window 001. Auditor MISSED this one (keep_as_translate_term/high). The §32 code collision guard caught it (chuẩn hóa colliding with normalization/standardization) and soft-gated it.
Two independent nets, each caught exactly one case. Root cause both times: first-write-wins canonical at entry creation; later/parallel better candidates parked as variants with no re-election round.

### 36.2 No-gold detection signature (pure internal data, $0)
Watchlist an entry iff: (target_variants contain a candidate != canonical) AND (collision flag from §32 OR audit_label == polysemy_or_context_dependent). Both harmful cases match; signature needs no gold. (canonical∉variants ALONE is too broad — 144 entries, known red herring.)

### 36.3 Blind re-election round (new dictionary-lifecycle step, post-Auditor pre-pack)
For watchlisted entries only, elect canonical from {current canonical} ∪ {target_variants} by evidence; LLM generates evidence, CODE decides by mechanical rule:
- (a) Back-translation election (local Gemma, $0, blind — no hint of which candidate is incumbent): back-translate each VI candidate -> EN; a candidate whose back-translation string-matches the source term (casefold/lemma-loose) beats one that maps to a DIFFERENT English concept. Resolves collision-type errors (chuẩn hóa -> normalization ≠ regularization; điều chuẩn -> regularization ✓).
- (b) Context majority vote (for polysemy-type where back-translation ties, e.g. population): for each real occurrence sentence in the book, ask blind "VI rendering of TERM in THIS sentence" (local, $0), majority wins (tổng thể expected on stats-context sentences).
- Safety gates, §29 style, all mandatory: (1) only signature-matched entries; (2) winner must not create a NEW target collision; (3) full decision log (candidates, back-translations, votes, evidence blocks). Election result propagates via the normal dictionary mechanics (per-window cache invalidation only where the term appears; registry-so-far carries to later chapters).

### 36.4 Product frame (one-button, per Thầy's requirement)
[DỊCH] -> translate -> score -> report contains: B/D/hygiene/tier-shares + auto WATCHLIST (context_sensitive + validator-rejects + masquerade suspects + re-election log) each with 3-5 EN-VI evidence sentence pairs from cascade marks. Human review = OFF-by-default toggle; when ON, pause at watchlist before finalizing. Rationale recorded: reviewers (incl. the user) cannot judge abstract term quality, but CAN judge bilingual evidence in context; and self-reported LLM "reasons" are post-hoc confabulation — behavior is measured by the cascade instead (no reason channel in Translator; its output contract stays translation-only).
- Residual risk stated honestly: a lone wrong canonical (no competing variant, no collision, no polysemy label) evades all nets; consistency makes it a 1-line fix whenever any reader finds it. Human gold errs too (heuristic->thực nghiệm).
- Open v2 measurement question (logged, not assumed): on collision terms, does a soft hint HELP or HURT vs no hint at all (§30 excluded mode)? Evidence so far: S1 followed the bad "chuẩn hóa" hint while free S0 sometimes chose gold "điều chuẩn". Measure with cascade, decide with data.

### 36.5 Sequencing
1) Finish exp_s0s1_builderv2_v1 (cascade STOP-B -> overlay -> prelim pair §35.9 -> prelim scoring). 2) Then implement §36.2+36.3 as a Builder-pipeline step + §36.4 watchlist in report/UI. 3) Re-run dictionary build for MLP as the §36 validation case: success = regularization->điều chuẩn and population->tổng thể elected mechanically with logs, zero regressions on the other 424 pack terms.


<!-- S37_POINTER -->
## 37 — Scoring framework SPUN OFF + Builder-V2 closing conditions

The thesis scoring framework (TC/TA/SF-BT/SF-QE/PJ + gates, hardware notes, literary roadmap) is specced in **tasks/TASK_EVAL_SCORING_V1.md** — eval work no longer appends here.

BUILDER-V2 CLOSES when all of: (1) §35.10 cascade STOP-B verified + overlay smoke on real marks; (2) §35.9 prelim S0/S1 run + 0-API scored; (3) §36 canonical re-election implemented + validated on the MLP dictionary. Then a final closing section summarizes outcomes and the file goes read-only.


## §35.13 STOP-B verified — MLP cascade production run ACCEPTED (Claude, 2026-07-03)

Independent verification on artifacts (NOT the report), full sweep no sampling:

- Hashes self-computed: frozen `64D989...` unchanged, workdb `968CD4...` unchanged → scorer inputs untouched; locked §35.8 B/D numbers CANNOT have moved.
- Full cross-check of ALL 2x2,487 decisions vs workdb `translation_runs.output_text`: 0 mismatches (supersedes CodeX's 40-sample audit).
- Quote-in-region validated for all T3 quotes: 0 real failures. 4 apparent misses (2/arm, all `mlp_b047` "Hàm *sigmoid*") are markdown-asterisk cases correctly matched via `target_quote_clean`; consequence is `target_quote_start_in_region=null` → UI must fall back to clean-text highlight for those (4/4,946 = 0.08%). Not a defect; note for overlay implementer.
- Every headline number recounted from decisions matches CodeX's report exactly (tier splits, adherence labels, backend split 1302+18 / 1140+13, escalation buckets, confidence all-high as predicted non-informative).
- T3 cache: exactly 2,473 `google/gemma-4-12b` rows, created 20:40-22:31 UTC = the run window. Paid fallback $0.008.
- Script skim: `run_experiment_cascade.py` opens DBs `mode=ro`, key precedence env -> KEY-2 -> KEY-1, no key logging.

New measured facts the block-level scorer could not see (display-layer lens, NOT thesis metrics):

- **Cascade-lens accepted-form rate** (per-occurrence, union scope 2,487): S0 0.7543 vs S1 0.8737, gap +0.119 — same direction as D (0.759->0.825), larger gap because per-occurrence localization removes block any-match generosity. Convergent evidence, report as audit of B/D, never as replacement (would be a post-hoc metric).
- **True masquerade suspects measured** (EV-D2L-10 goal): S0 42 / S1 47 of 2,487 (~1.7-1.9%) → stray-credit inflation in B/D is bounded tiny; the locked numbers stand validated.
- Behavioral memory signature: S1 needed fewer T3 calls (1,153 vs 1,320), higher T2 share (53.6% vs 46.9%), C0 nearly halved (425 vs 713) — memory makes output land on accepted forms more often, visible even in the plumbing.

Committed: cascade script + tests + audit40 + run logs. Large regenerable artifacts (2x8MB decisions, 18MB summary, 11MB T3 cache) left untracked per metrics-csv precedent; regenerable at $0 from cache/workdb.

Remaining for closure (§37): overlay smoke on real marks -> §35.9 prelim pair -> §36.


## §35.14 Soft-tier anchor probe — does deviation freedom hurt TC? (Claude + user, 2026-07-03)

User question: if low-tier terms get deviation rights (soft hints, no hard forcing) and the Translator cannot write back to the dictionary, does window-N's improvisation create inconsistency that window-N+1 can't know about — hurting TC? And does the pack show the Translator multiple variants or one representative form?

Evidence pulled from the REAL S1 prompts (llm_call_cache, created_at 2026-07-02, all 62 MLP windows):
- Pack = 3 compartments: MANDATORY (exact form required) / PRESERVE (keep source token) / CONTEXT-SENSITIVE ("do not force" = deviation rights). **Every term carries exactly ONE representative VI form; 0/62 prompts contain any multi-variant line; hints are byte-stable across all 62 windows.** The variant inventory exists only in the notebook (Builder) and the scoring ruler — the Translator never sees it.
- The user's mental model is CORRECT: windows are stateless; the frozen pack is the only cross-window channel; no write-back channel exists.

Measured TC impact (D per-term forms_used from metrics_mlp.json, majority-share occurrence-weighted, 42 soft-hint terms):
- soft-hint terms: S0 0.8927 (289 occ) -> S1 0.9658 (292 occ) — the FREE tier is the most consistent group in the system, beating even S1's other terms (0.9195).
- S1 drift on soft terms (7 terms) is benign morphology (sample: mẫu/mẫu dữ liệu; objective: mục tiêu/hàm mục tiêu).

Design conclusions (locked):
1. **Consistency comes from the shared anchor, not from coordination.** 62 independent windows pulled toward one stable suggestion ≈ high TC even at ~95% compliance. Scattered defections don't kill TC; a missing or mid-run-changing anchor would.
2. **No mid-run write-back stays locked**: with conc-3 it would be first-write-wins at the translate layer (the same disease §36 cures at the Builder layer), plus non-reproducibility and cache invalidation. A Translator improvement is not lost — it lives in the output, the cascade localizes it, and the legitimate promotion channel is between-run re-election (§36).
3. **Never advertise variants in the prompt**: listing 3 accepted forms invites variant churn (TA-happy, TC-poor scenario B). One representative form + soft deviation rights is measured-best on both axes.
4. population AND regularization sat in the soft tier (§30 gate worked as designed — the bad canonical was not hard-forced).

Reproducibility check bundled with this probe: re-ran `python -m pipeline.scripts.score_run --db data/jobs/exp_s0s1_full/memory.sqlite3 --experiment exp_s0s1_builderv2_v1 --chapters d2l_multilayer_perceptrons --profile technical_d2l_v1 --gold-variants data/eval/d2l_gold_variants.csv` → B 0.7580/0.7657, D 0.7590/0.8253 — identical to the locked §35.8 report to 4 decimals. One command, $0, fully automatic (feeds the one-button report).

Per-occurrence pair TC-Occ/TA-Occ pre-registered for preliminaries in TASK_EVAL_SCORING_V1 §8 (MLP numbers retrospective-only).


## §35.15 UI overlay wiring + materialize (CodeX task, 2 STOPs — closes the §37 "overlay smoke" condition)

Context: STOP-B artifacts verified (§35.13). Backend already accepts `cascade_report` and merges marks (`mark_source=cascade_t2/cascade_t3_llm`, `scored=false`); frontend doesn't pass it yet; `_JOB_REPORT_MAP` is hardcoded; overlay is re-composed (incl. T3 quote re-locate) on every chapter load. CodeX's status survey accepted; plan amended by Claude as below.

LOCKED design decisions (do not re-litigate in implementation):
1. **Dedupe rule (one line):** an occurrence with a cascade decision → the cascade mark REPLACES any overlapping legacy `surface_form` span. `surface_form` survives only where cascade has no coverage (terms outside union scope). A same-occurrence collision between `cascade_t2` and `cascade_t3_llm` is IMPOSSIBLE by construction (`resolved_by` is exclusive) — if detected, FAIL LOUD (assert + report), never silently dedupe. Cross-term physical overlaps between cascade marks: keep both, flag in audit (masquerade/watchlist material — deleting them destroys evidence).
2. **No mid-term overlay cache.** Materialize is the only mechanism (cache-by-mtime solves the same problem with a second state machine; rejected).
3. **`_JOB_REPORT_MAP` hardcode replaced** by convention/manifest: report paths derive from `experiment_id` + a manifest file sitting next to the artifacts (e.g. `data/reports/<experiment_id>/manifest.json`). No per-experiment code edits.
4. **Display/score boundary preserved:** all cascade-derived marks stay `scored=false`, `forms_source="cascade_report"`. Overlay work NEVER re-scores B/D and NEVER calls any LLM (Gemma/GPT) — every quote/position comes from the frozen cascade JSONs. $0 task.
5. **Mark provenance is a UI feature (user request):** every mark carries `located_by` shown in the tooltip as a "LOCATED BY" row + small badge, values:
   - `code_exact` (cascade_t2) — "định vị bằng code, khớp chính xác"
   - `ai_locate_local` (cascade_t3 via google/gemma-4-12b) — "AI định vị (Gemma, cục bộ)"
   - `ai_locate_fallback` (t3 gpt_fallback) — "AI định vị (GPT dự phòng)"
   - `block_detect` (legacy surface_form) — "phát hiện mức block (không có vị trí chính xác)"
   Plus optional flags rendered as badges when true: `masquerade_suspect`, `clean_text_fallback` (the 4 b047 markdown cases), `not_rendered` (term absent — render as gutter note, not a span). Do NOT display Gemma's confidence field (measured non-informative, §35.12 — always "high").

### STOP-C1 — wire-through (small)
- Frontend passes `cascade_report` + `experiment_id` for the exp job; backend applies dedupe rule (1); manifest mechanism (3) replaces `_JOB_REPORT_MAP`.
- Acceptance: UI shows cascade marks for MLP on BOTH arms; per-`mark_source` counts reported and reconciled against cascade decisions (S0: t2 1167 / t3 1315; S1: t2 1334 / t3 1151); zero t2/t3 same-occ collisions (assert ran); scorer layer loads metrics_mlp.json via manifest. STOP — report counts to Claude, no commit.

### STOP-C1 ACCEPTED (Claude verified, 2026-07-03)

Round 1 caught one real bug: heuristic `_cascade_decisions` walker scooped `/t3_run_stats/*/sample_marks` (10 display-sample records, occ_ids duplicating real decisions) → opaque skipped:17 that didn't reconcile (4,967+17 ≠ 4,974). Harmless today only because samples lack `config` — a future format tweak would have made silent duplicate marks with no collision alarm. CodeX fixed: strict schema-only extractor (top-level `decisions` | `reports/<config>/decisions`), `skipped_by_reason`, sample_marks regression test, real-overlap dedupe test, gpt_fallback marks-vs-calls audit note.

Claude verification (independent, real artifacts): re-ran pytest (20 passed); composed the real overlay via `load_registry_overlay` directly → audit identical to CodeX's (loaded 4,967 / skipped 7 = not_rendered only; t2 2,501; t3 2,466; located_by local 2,437 + fallback 29 = 31 calls − 2 not_rendered; masquerade 42/47; clean_text 2/arm). Dedupe now REAL: 2,925 legacy same-term overlapping spans replaced (was 0 — legacy occurrences carried no `id`; fixed via bucket term-id); post-merge invariant checked by independent scan: 0 same-term surface_form spans overlapping cascade spans; survivors S0 1,376 / S1 1,389 = legacy-only coverage, per design. Cross-term overlaps 95 kept + flagged.

Numbers with two truths (documented): gpt_fallback = 18/13 CALLS (§35.13) vs 16/13 MARKS (2 S0 fallback calls concluded not_rendered — nothing to draw).

Browser visual smoke deliberately deferred to STOP-C2 acceptance (Claude runs it on the materialized artifact).

### STOP-C2 — materialize UI-ready overlay
- Emit `overlay_mlp_S0.json` / `overlay_mlp_S1.json` (UI-ready): spans merged + deduped + priority applied; the 4 clean-text-fallback offsets resolved ONCE offline (verified artifact == displayed artifact, byte-for-byte); every mark carries `located_by`, flags, `occ_id`, `term_id`.
- Audit stats block inside each file: counts per located_by, deduped-surface_form count, cross-term overlap count (flagged, kept), clean_text_fallback count (expect 2/arm), not_rendered count (5 S0 / 2 S1), gpt_fallback count (18 S0 / 13 S1).
- UI loads the materialized artifact only; no runtime re-compose, no runtime quote re-locate.
- Acceptance (= §37 overlay smoke): total marks per arm = 2,487 − not_rendered; stats reconcile with §35.13 numbers; Claude spot-checks rendered marks (incl. one b047 clean-text case and one gpt_fallback case) in the real UI. STOP — Claude verifies then commits.


### STOP-C2 ACCEPTED — overlay smoke §37-① COMPLETE (Claude verified, 2026-07-03)

Backend verification (independent):
- Materialized `overlay_mlp.json` recounted from the file itself: audit identical to STOP-C1 compose; per-arm marks S0 1167/1315/1376, S1 1334/1151/1389; flags carried (masquerade 42/47, clean_text 2/2, gpt marks 16/13); dedupe invariant scan on the FILE: 0 violations.
- Deep set-compare: fresh compose (`prefer_materialized=False`) vs materialized file = 7,593 mark tuples IDENTICAL both directions (0 diff). Verified artifact == displayed artifact.
- Live HTTP route (restarted backend): returns `materialized_loaded_from=overlay_mlp.json`, same audit. (Stale pre-C1 backend was still running on port 5000 — the earlier UI screenshot came from OLD code; killed PID and restarted. Reminder: after CodeX changes backend code, Flask must be restarted — no auto-reload.)

Browser visual smoke (Claude, real UI via preview: Flask :5000 + prototype :8765, thesis:exp_s0s1_full, MLP chapter):
- 9,640 highlight elements render from the materialized overlay across S0/S1 panels.
- b047 clean-text case: mark `**Hàm *sigmoid*` renders; hover card shows `located by: AI locate (Gemma)` + flags `clean_text_fallback, cross_term_overlap`. ✓
- GPT-fallback case (dropout_b017 `chuẩn hóa dropout tiêu chuẩn`): hover card shows `located by: AI locate (GPT fallback)` + flags `gpt_fallback, cross_term_overlap`, CASCADE badge on card. ✓

Committed: materialize script + overlay service/tests + manifest. Overlay artifacts (8.6M + 2×4.8M) left untracked per large-regenerable precedent (one command re-emits from cascade JSONs).

Known nit (non-blocking, logged): 139 fully-duplicate legacy `surface_form` mark tuples pre-exist within the block-detect layer (7,732 list entries vs 7,593 unique) — cosmetic, cascade marks unaffected (reconcile exactly); candidate cleanup in a later UI pass.

§37 closing conditions status: ① cascade STOP-B + overlay smoke ✅ DONE | ② prelim pair (§35.9) — next | ③ §36 re-election — queued.


### §35.15-F1 — follow-up (bundle with prelim overlay pass): experiment binding per job, not global param

Two real user-hit symptoms on 2026-07-03, same root cause (frontend `experiment_id` is a manual URL param persisted GLOBALLY in localStorage):
1. Opening `exp_s0s1_full` WITHOUT the param → no manifest resolution → score_status job_not_found, cascade not_requested → everything unscored, zero cascade marks (user assumed the feature was broken).
2. After setting the param once, it leaks into ALL thesis jobs → `d2l_p1` (experiment d2l_p3) filtered by exp_s0s1_builderv2_v1 → 0 translations → S0/S1 panels and runtime marks vanish from a previously-working view.

Fix (small): backend datasets list returns each job's `experiment_id` (or manifest presence); frontend passes it automatically per job; localStorage keyed per job_id if kept at all; URL param demoted to a debug override. One-button product rule: users never supply routing params by hand.


### §35.15-F2 — follow-up (bundle with F1): mark COLOR encodes consistency verdict only, never provenance

User-hit confusion 2026-07-03: source-panel `softmax regression` renders GREEN (hl-status-consistent from score report) while its cascade target mark `hồi quy softmax` renders YELLOW — because cascade marks carry pipeline-stage statuses (`rendered` for t2, `localized` for t3) that have NO CSS rule and fall through to the default highlight color. Two taxonomies (consistency verdict vs localization stage) are sharing one color channel. Measured mix in overlay_mlp.json: surface_form carries real verdicts (1,437 consistent / 879 drift / 449 unscored) while all 4,967 cascade marks carry only stage labels.

Locked design:
1. Color channel = consistency verdict ONLY: green consistent / red drift / gray-blue unscored — applied to cascade marks too, using the term's per-config verdict already present in the overlay data (tooltip FORMS_USED / overlay_status_by_config). Provenance NEVER changes fill color; it lives in LOCATED BY + flags (already shipped).
2. Cascade marks on scope-only terms with no registry verdict (the §8b undetected group): neutral "localized-only" color of its own. Long-term: TC-Occ (EVAL §8) becomes the verdict source for these — cascade localizes, TC-Occ judges, color displays.
3. Add a small on-screen color legend. The system author had to ask what the colors mean; end users have no chance.
4. Regenerate materialized overlays after the change (status field per mark) — one script run.


### §35.15-F3 — term focus mode (user request, bundle with F1/F2)

Goal: click a term -> that term stays visually prominent across ALL blocks/panels while scrolling, so the user can audit one term through the whole chapter.

Behavior (frontend-only, read-only, no backend change — marks already carry `id` + `occ_id`):
1. Click any mark (or a glossary panel row) -> FOCUS MODE on that term:
   - all occurrences of the term get a strong emphasis ring/outline + subtle glow, in source panel AND both S0/S1 panels;
   - all OTHER marks dim (reduced opacity), block text stays readable;
   - focus persists across scrolling and block navigation within the chapter.
2. Floating focus chip (sticky, e.g. top of center pane): term EN -> VI, occurrence count, and PREV/NEXT buttons that scroll-jump to the previous/next occurrence (wrap around; count position "3/9"). Esc, clicking the chip's ✕, or re-clicking the focused term exits focus mode.
3. Identity: group by mark `id` (cascade marks already share glossary ids via C1 lookup); fallback grouping by normalized `source_term` for scope-only ids so cascade + legacy layers focus together.
4. MUST NOT fight F2 color semantics: emphasis = ring/outline/glow + dimming others; never change the verdict fill color of the focused marks.
5. Tooltip stays functional in focus mode; clicking a DIFFERENT term switches focus to it.

Acceptance: focus `softmax regression` in MLP -> 9 occurrences ringed across panels, others dimmed; NEXT cycles through all 9 in order (S0/S1 scroll in sync to the block); Esc restores normal view; verdict colors unchanged under emphasis.


### F1+F2-R+F3 ACCEPTED (Claude verified, 2026-07-03 evening)

Round 1 of F2 was REJECTED on arithmetic evidence: cross-tab showed 100% t2->consistent / 100% t3->localized_only (pure tier rename), zero drift cascade marks; counter-examples gl_bias S0 (bias/độ chệch/độ lệch yet 6/10 green) and gl_backpropagation (11/16 green). CodeX rebuilt per F2-R spec.

Final verification (independent, artifacts + live UI):
- **F2-R**: cross-tab no longer tier-degenerate (t2: 1754 cons/438 drift/309 loc-only; t3: 1180/1110/176); per-(term,arm) verdict uniformity: 0 violations; canaries all correct — bias S0 DRIFT, backpropagation S0 DRIFT, activation functions S0 CONSISTENT (plural collapsed), activation function S0 DRIFT (bare `hàm` NOT collapsed upward), mlp S1 CONSISTENT (`(mlp)` gloss collapsed). CodeX improved on the spec: containment-collapse is DIRECTIONAL — only longer-superset forms collapse into the majority form; short fragments never collapse upward (fragment-trap §8b honored). Verdict rules: <2 localized occ -> localized_only; ≥2 occ: 1 collapsed form -> consistent, else drift. Labeled TC-Occ-display (display layer only, never a thesis number).
- Source-panel marks now prefer cascade verdict when the term has cascade coverage (worst-of across arms when arms disagree, e.g. mlp S0 drift + S1 consistent -> source shows drift); legacy fallback otherwise.
- **F1**: fresh browser session with NO url param: datasets carries experiment_id only for exp_s0s1_full; frontend auto-attaches it to overlay calls (network log verified); other jobs unaffected.
- **F3**: source-side click now yields chip 1/16 (manual DOM query 16 focus elements); dimming 9,624 marks; Esc clears; chip falls back to glossary canonical when clicked from source side.
- Tests: 27 passed (re-run by Claude). Overlay regenerated; audit counts unchanged (4,967/7).

Ops reminder recorded twice now: CodeX verify runs leave EXTRA backend processes on :5000 (two rounds: 2 PIDs, then 1 stale) — stale code serves silently. Always `netstat :5000` + kill before browser verification.

<!-- S35_9_GO -->
### 35.9-GO — user approved, frozen inputs verified (Claude, 2026-07-03)

User question resolved before GO: "co can chay lai Builder cho preliminaries khong?" -> KHONG. Notebook prelim la san pham Builder that (DEV chapter, C2->C3->C3.5->S29->S30), da pin trong S35.2. Git log verified: khong co commit nao dung Builder/Auditor/Translator code tu sau khi cap MLP chay (moi commit pipeline/ sau do la eval/cascade/overlay/materialize: e5a66a6, a91e67c, dd84e6d). Re-build "cho chac" = re-roll non-determinism + pham S35.2 freeze + mo coi hoa ket qua MLP da duyet -> REJECTED. Nhu cau "chay lai Builder xem co tai lap khong" da co cho dung: S36 validation case (dieu kien (3) cua S37).

0-API verify (chay truoc GO, khong sua gi):
- notebook prelim data/reports/builder_v2_c35_decollision/notebook_decollided.json: sha256[:16] 308B6C6C28A9E562, 340 entries; pack policy trong preflight khop S30: 196 hard + 26 preserve + 37 context_sensitive = pack 259, report_only 78, repair_queue 3.
- notebook MLP (doi chieu, khong dung o buoc nay): E6EFCBA7B0993FFC, 546 entries.
- frozen DB d2l_p1: 64D98965F8859869 — dung baseline.
- workdb exp_s0s1_full: translation_runs S0=475/S1=475 (MLP-only), 0 dong prelim, 0 pack pk_*_prelim* -> resume path sach, khong co stale-pack de reuse.
- preflight_preliminaries.json: experiment_id/notebook/budget 1500/45 windows/348 blocks khop task; zero_api=true; frozen hash before==after.

GO: CodeX thuc thi S35.9 dung nguyen van (S0 replay-cache truoc, S1 sau; nominal ~$0.10-0.15, cap $0.762, abort neu incremental > $0.40). STOP sau run, KHONG score, KHONG commit. Claude cham 0-API (S35.8 command, chapters d2l_preliminaries) + doi chieu pre-registered expectations trong S35.9. Day cung la lan DAU TC-Occ/TA-Occ chay chinh danh (EVAL S8) — nhung chi SAU khi headline B/D v3 duoc cham va ghi nhan truoc.

<!-- S35_9_GO2 -->
### 35.9-GO-2 — ERRATUM: notebook prelim phai la notebook_promoted.json (CodeX catch, Claude verified, 2026-07-03)

CodeX phat hien TRUOC khi chay prelim (chua co output nao duoc nhin): GO-1 pin `builder_v2_c35_decollision/notebook_decollided.json` — day la artifact TRUOC S29. Claude verify doc lap tren file:
- prelim decollided: KHONG co `ledger_promotion_version`; MLP decollided: CO `c4_keep_source_v1` (S29 inline) -> hai chuong lech pipeline stage neu chay theo GO-1. CONFIRMED.
- `notebook_promoted.json` sha256 557fb83eced6b2a2... (khop so CodeX). Diff vs decollided = 17 entries nhung CHI 1 doi canonical: `gradient` "đạo hàm riêng" -> "gradient" (audit polysemy->keep, injection context_sensitive->translate, inject_as_hard_canonical=true). 16 entry con lai = metadata (don `decollision` ve null; ghi `ledger_promotion` blocked/held cho one/shape/tensor — dung 3-gate S29: promote 1 / block 2 / hold 1). CodeX noi "khac dung 1 entry" — dung ve NGU NGHIA, thuc te 17 entry cham metadata.
- He qua pack (CodeX chua neu): gradient len hard -> pack 197 hard + 26 preserve + 36 soft = 259 (tong khong doi).

Phan xu ve freeze S35.2: day la WIRING BUG trong chinh ban pin (pin mau thuan voi pipeline policy da khoa C2->C3->C3.5+S29->S30; MLP da chay voi S29). Sua truoc khi chay chuong bi anh huong, chua nhin ket qua prelim nao -> KHONG phai tuning-on-test; giu nguyen pre-registered expectations. Chay theo GO-1 moi la sai (nhoi loi gradient da biet vao S1 prelim, hai chuong khac policy).

Preflight v2 (Claude chay, 0-API): `preflight_preliminaries_v2.json` — notebook_promoted, 45 windows / 348 blocks, pack 197/26/36, S1 prompt est 55,904 tok (cu 55,992 — chenh khong dang ke), frozen hash before==after 64D989, zero_api=true.

GO-2 (thay the notebook path cua GO-1, moi thu khac giu nguyen): CodeX chay S35.9 voi `--memory-notebook data/reports/builder_v2_c35_decollision/notebook_promoted.json`, doi chieu preflight_preliminaries_v2.json. Cost/cap/abort/STOP rules y nguyen GO-1.

<!-- S35_9_RESULTS -->
### 35.9-RESULTS — prelim pair verified + scored 0-API (Claude, 2026-07-04)

**Verify run (doc lap tren workdb, truoc khi cham):** frozen DB 64D98965 nguyen ven; workdb = 823 rows/arm (348 prelim moi + 475 MLP cu, digest MLP outputs 6afc26b5 — khong bi dung); 0 output rong; hygiene still_bad=0 (1 flagged/1 reask/1 fixed moi arm); calls 46 = 45 window + 1 reask; cost incremental ~$0.0856 << cap $0.762. Report CodeX khop workdb 100%.

**Diem (metric v3 khoa, lenh S35.8, metrics_preliminaries.json):**
| | S0 | S1 | delta |
|---|---|---|---|
| B/TA (gold, 497 occ) | 0.6660 (331/497) | 0.6036 (300/497) | **-0.0624** |
| D/TC (95 hard terms) | 0.8000 | 0.8526 | **+0.0526** |

**Doi chieu pre-registered (S35.9):** D: S1>S0 — **PASS** (+0.0526, cung huong MLP +0.066). Hygiene still_bad 0 — PASS. Windows skipped 0 — PASS. B "small positive move" — **FAIL** (-0.062). Ghi trung thuc, KHONG sua metric, KHONG doi headline.

**Chan doan B (diagnostic, tu occurrence_audit — khong phai patch):** cu roi do MOT term chi phoi: `vector` (80 occ = 16.1% toan mau so). Gold style-guide d2l-vn: vector -> "vector" (giu nguyen). Notebook Builder: vector -> "vectơ" (chinh ta VN, hard/high). S0 tron lan vector 74 / vectơ 35 -> duoc 53/80 credit; S1 vang loi tu dien vectơ 107 / vector 2 -> 2/80. Loai rieng vector: S0 278/417=0.6667, S1 298/417=**0.7146** (+0.048 — dung pattern MLP). Guong doi xung cung ton tai: `elementwise` notebook TRUNG gold -> S0 0/22 vs S1 19/22 (+19). Co che: B do muc DONG THUAN notebook-vs-gold xuyen qua su vang loi cua S1; tren prelim mot bat dong chinh ta tan suat cao lam lech ca headline. Day la truong hop "gold = style guide" (S33) co dong tai 1 term, va la dung input cho S36 re-election + cap TA-Occ-vs-own-notebook (EVAL S8, production mode "tuan thu tu dien tu xay").

**Gate `scope_equals_translation_runs=false` (ca 2 arm):** BENIGN — verified code (d2l_translate_score.py ~776): gate so scope_ids (348 block prelim) voi TOAN BO outputs cua experiment (823 gom ca MLP). Tu khi workdb chua 2 chuong, moi lan cham per-chapter deu false; hai arm cung scope nen so sanh van cong bang. Khong sua gate giua experiment; ghi nhan de bao cao.

**Ghi chu CodeX "tensor bậc k cao":** dong that o autograd_b030 (S1): "Với `y` và `x` bậc cao hơn và nhiều chiều hơn..." — van doc duoc, xep vao ro PJ/fluency cham sau, khong lien quan B/D.

**Next:** cascade localize prelim (T1 bge-m3 + T2 code + T3 gemma local $0, GPT fallback validator-reject-only ~vai call) -> TC-Occ/TA-Occ CHINH DANH lan dau (predictions P1-P3 EVAL S8) + materialize overlay prelim cho UI (F1 binding da san).

<!-- S35_16_CASCADE_PRELIM_TASK -->
## 35.16 — TASK for CodeX: cascade localize preliminaries (S0+S1) + materialize overlay prelim

User approved 2026-07-04. Muc dich: (1) marks EN<->VI 1:1 cho prelim tren UI nhu MLP; (2) cung cap decisions de Claude tinh TC-Occ/TA-Occ CHINH DANH lan dau (predictions P1-P3 da dang ky truoc trong TASK_EVAL_SCORING_V1 §8 — CodeX KHONG cham, KHONG doc lai predictions truoc khi chay; chi chay may).

May moc DA PROVEN o §35.10-35.13 — KHONG doi logic cascade/prompt/threshold nao. Chi doi tham so chuong.

### Buoc 1 — Preflight (0 API, 0 local LLM)
Nhu lenh §35.10 STOP-A nhung:
- `--chapter preliminaries`
- `--cache-dir data\eval\embed_cache\cascade_exp_prelim` (MOI — khong dung chung voi mlp)
- `--notebook data/reports/builder_v2_c35_decollision/notebook_promoted.json` (**GO-2 notebook — BAT BUOC**, khong phai notebook_decollided)
- `--gold-variants` va cac flag khac y het MLP; `--out-dir dataeports\exp_s0s1_builderv2_v1`; `--llm-cache` dung chung sqlite (key moi theo occ, khong dung do MLP).
Bao cao: denominator per arm (union pack∪gold — se KHAC 2,487 cua MLP, la con so cua prelim), t2/t3 split, breakdown C0/C1/C2+, embed cache stats. Neu denominator per arm > 3,500 hoac t3_residual > 2,500: STOP hoi lai (sanity — prelim nho hon MLP, khong the lon hon nhieu).

### Buoc 2 — Run that
- T3 = **local Gemma** nhu §35.12b: `--t3-backend local-lmstudio`, gemma-4-12b, concurrency 3, temp 0, repeat_penalty 1.0, seed 20260612, json_schema. LM Studio phai load san bge-m3 + gemma-4-12b truoc khi chay.
- GPT fallback CHI khi validator-reject (`--gpt-fallback-on-validation-error`), key thu tu env -> KEY-2 -> KEY-1, cap fallback $0.10 (MLP thuc te ~$0.005), bao ro TUNG call fallback (occ_id + ly do).
- Uoc thoi gian: MLP 2,473 call ~ 2h21m; prelim residual nho hon -> du kien duoi 1h30. Chay tuan tu S0 roi S1.
- **Guard bat bien:** workdb + frozen DB mo mode=ro; hash workdb TRUOC == SAU cascade (cascade khong duoc ghi gi vao DB); frozen 64D989 nguyen; workdb hash se KHAC 968CD4EB thoi MLP (da them prelim rows o §35.9 — do la expected, ghi hash moi vao report). Khong log key. Khong hien thi Gemma confidence (non-informative, da chot §35.12).

### Buoc 3 — Materialize overlay prelim
- `python -m pipeline.scripts.materialize_thesis_overlay --job-id exp_s0s1_full --experiment-id exp_s0s1_builderv2_v1 --chapter-id d2l_preliminaries ...` -> `overlay_preliminaries.json` + per-arm, cung out-dir.
- Update `manifest.json`: them entry prelim (cascade reports + overlay) ben canh MLP. KHONG dung den entry MLP.
- Overlay statuses phai qua duong F2-R verdict (TC-Occ-display directional collapse) nhu MLP — khong duoc quay ve tier-rename.

### Buoc 4 — STOP
Khong score, khong commit, khong doc P1-P3. Bao cao: per-arm denominator/resolved_by/masquerade/validation_error/fallback calls/elapsed; duong dan artifacts; audit merge (loaded/skipped_by_reason/deduped/cross_term) cua overlay prelim; xac nhan manifest. Claude se: full-sweep verify decisions-vs-workdb (nhu verify_stop_b), recount headline, tinh TC-Occ/TA-Occ chinh danh + doi chieu P1-P3 (co fragment filter theo EVAL §8), browser smoke UI prelim, roi moi commit.
<!-- S35_16_RESULTS -->
### 35.16-RESULTS — cascade prelim + overlay ACCEPTED (Claude verified, 2026-07-04)

**3 self-fix cua CodeX — review tung diff, CHAP NHAN ca 3:**
1. `--artifact-prefix` (run_experiment_cascade.py): default cascade_<chapter>, sanitize; khong co no thi prelim DE cascade_mlp_* — catch dung. Artifacts ra cascade_preliminaries_*.
2. materialize khong truyen cascade_report vao composer (bug lan dau ra cascade_rendered_marks=0) — fix + manifest per-chapter (`chapters.<id>.reports`), top-level giu MLP; test moi cover chapter-preference; resolver merge `{**reports, **chapter_reports}` backward-compat.
3. LM Studio HTTP 500 sau ~1h -> transport error di qua GPT fallback thay vi sap run. Verified: error path return TRUOC _store_cached -> KHONG cache loi, rerun van thu Gemma truoc. Gap con lai (connection error ngoai raise_for_status van crash) ghi nhan, khong chan accept.

**Full-sweep verify (khong sampling):** 2x1848 decisions vs workdb: 0 mismatch. Denominator/resolved_by/masquerade khop bao cao CodeX tung so (S0 921 t2 + 927 t3, S1 1027 + 821; fallback 29+1=30 call ~$0.0072; validation_error 0). Workdb hash 92229381...E8C13F42 khop truoc==sau; frozen 64D989 nguyen. 6 ca quote S0 khong khop tho = markdown formatting (clean-strip khop het, cung lop 4 ca b047 MLP) — benign. t3 marks = t3 decisions - not_rendered dung ca 2 arm (922=927-5, 812=821-9).

**Overlay + UI smoke:** route tra dung file materialized (materialized_loaded_from=overlay_preliminaries.json), mark source khop (756/921/922, 742/1027/812), status = verdict F2-R khong suy bien (S0 1421 cons/702 drift/113 loc/363 unscored; S1 1517/583/113/368 — S1 xanh hon dung huong TC-Occ). MLP route van overlay_mlp.json (3874 marks S1). Browser: prelim b002 render 6424 span 4 lop verdict; focus click "vector" -> chip 1/154, 154 match/6270 dim. 24+3 test CodeX re-run pass.

**Diem chinh danh TC-Occ/TA-Occ ghi o TASK_EVAL_SCORING_V1 §8c** (P1 PASS / P2 PASS / P3 vi pham o o TA — dieu tra xong: ruler divergence, khong phai harness bug). tc_ta_occ_preliminaries.json committed.

**§37 status: ① DONE | ② DONE (§35.9 run + scored + §35.16 cascade/overlay/TC-Occ-TA-Occ chinh danh) | ③ §36 — NEXT.**
<!-- S36_6_IMPL_TASK -->
### 36.6 — TASK for CodeX: implement blind canonical re-election (§36.2+36.3) + validation on MLP dictionary

User approved 2026-07-04. Day la dieu kien ③ cuoi cung cua §37. Thiet ke da LOCK o §36.1-36.5 — implement dung nguyen van, khong sang tao them signature/rule moi. Toan bo LLM = local Gemma ($0); KHONG can GPT trong step nay.

**Kien truc:** script moi `pipeline/scripts/builder_v2_reelection.py` — dictionary-lifecycle step doc lap, post-Auditor pre-pack. INPUT: notebook JSON (audited/decollided/promoted). OUTPUT: `notebook_reelected.json` + `reelection_log.json` vao out-dir rieng. KHONG ghi DB (frozen mode=ro chi de doc occurrence sentences); KHONG dung den artifact exp_s0s1 nao — notebook reelected la artifact MOI cho run tuong lai, khong ghi de notebook cua experiment da dong.

**Buoc 1 — Watchlist (§36.2, thuan code, 0 LLM):**
- Signature dung nguyen van: (target_variants chua >=1 candidate != canonical) AND (co collision flag tu §32 detector OR audit_label == polysemy_or_context_dependent). Assert theo LABEL/flag co san trong notebook + §32 module — TUYET DOI khong hardcode ten term (code-never-does-language-work).
- Chay tren CA HAI notebook dong bang: MLP `builder_v2_mlp_c35/notebook_decollided.json` va prelim `builder_v2_c35_decollision/notebook_promoted.json`. Bao: watchlist size tung notebook, danh sach entry_id + ly do match (variant-competitor + collision/polysemy). Ky vong forensic §36.1: MLP watchlist PHAI chua `population` va `regularization` (neu khong -> signature sai, STOP).

**Buoc 2 — Election (§36.3):**
- (a) Back-translation, BLIND: moi VI candidate ({canonical} ∪ variants, dedup casefold) gui rieng cho Gemma "dich cum nay sang EN" — prompt KHONG chua source term, KHONG noi candidate nao la incumbent. Code so ket qua voi source term: match casefold/lemma-loose (singular/plural don gian, mechanical). Candidate match > candidate khong match. Nhieu candidate cung match hoac 0 match -> tie -> (b).
- (b) Context vote: lay cac cau nguon that chua term (tu frozen DB, mode=ro; cap 30 cau, thu tu block_id deterministic), hoi Gemma blind "trong cau nay TERM nen dich la gi" (khong dua danh sach candidate de tranh anchor — doi chieu output voi candidate set bang casefold-containment sau). Majority wins; hoa -> giu incumbent + log unresolved_tie.
- Gemma profile bat buoc (memory da chot): temp 0, repeat_penalty 1.0, seed 20260612, json_schema, local endpoint. Parse fail -> retry 1 lan -> van fail thi entry do log `election_error`, GIU incumbent (khong bao gio doi canonical vi loi ky thuat).
- 3 gate §36.3 deu bat buoc: chi entry trong watchlist; winner khong tao collision MOI (re-run §32 detector tren notebook sau khi ap winner — vi pham thi revert entry do + log blocked_new_collision); log day du per entry (candidates, back-translations, votes + evidence block_ids, gate outcomes, winner, changed yes/no).
- Winner == incumbent -> log `confirmed`, khong doi gi.

**Buoc 3 — Validation case (§36.5-3, tren MLP notebook):**
- PASS khi: `regularization` -> canonical "điều chuẩn" VA `population` -> canonical "tổng thể" duoc bau BANG MAY voi log day du; VA moi entry ngoai watchlist canonical bat bien (assert bang diff toan notebook); VA moi thay doi khac trong watchlist (neu co) duoc liet ke rieng cho Claude review — khong tu ket luan tot/xau.
- Chay them tren prelim notebook_promoted (sanity): ky vong `gradient` giu "gradient" (da sua §29), khong co doi loan.

**STOP-A** sau Buoc 1 + estimate so call Gemma cho Buoc 2 (watchlist x candidates + context votes; du kien vai tram call, ~phut). **STOP-B** sau Buoc 2+3: bao ket qua + log + diff; KHONG commit. Claude verify (doc lai log, tu diff notebook, recount watchlist) roi moi commit. Out dirs: `data/reports/builder_v2_reelection_mlp/`, `..._prelim/`.

**Ngoai scope (de sau, khong lam trong task nay):** §36.4 watchlist-report/UI wiring; ap dung reelected notebook vao run dich moi; open question soft-hint-vs-no-hint.
<!-- S36_6_STOP_A -->
### 36.6-STOP-A — watchlist ACCEPTED, GO election (Claude verified, 2026-07-04)

Verify doc lap: recount signature §36.2 voi co §32 tinh SONG qua notebook_entries_to_term_rows (bai hoc: co collision KHONG luu tinh trong notebook, phai goi module that — lan recount dau cua Claude doc tinh nen thieu 8 entry, cach CodeX dung):
- MLP 37/37, prelim 26/26, diff NONE. Reasons: MLP 29 polysemy + 8 collision; prelim 22 + 4.
- Canary §36.1 du: population (quần thể vs tổng thể, audit_polysemy), regularization (chuẩn hóa vs điều chuẩn, collision_soft_fallback "unresolved shared canonical").
- Estimate khop phep cong tu watchlist.json: MLP 109 bt + 411 ctx cap = 520; prelim 57 + 154 = 211. $0 local, ~10-20 phut moi notebook.
- Code: khong hardcode term (canary la CLI param --expect-watchlist); zero_api=true; frozen DB ro, hash nguyen. 4+1+8 test pass (Claude re-run).

GO STOP-B: implement + chay election mode (back-translation blind -> tie -> context vote, 3 gates, log day du) tren MLP roi prelim, dung §36.6 Buoc 2+3. STOP sau khi co ket qua + diff notebook; khong commit.
<!-- S36_6_STOP_B_R1 -->
### 36.6-STOP-B ROUND-1 — mechanism CHAY DUOC, ket qua KHONG PASS validation; chan doan 3 loi co che (Claude verified, 2026-07-04)

**Verify doc lap:** diff toan notebook ca 2 bo: 0 flip ngoai watchlist (dung gate). Flip counts khop bao cao (MLP 8, prelim 7). Frozen DB 64D989 nguyen. 13 test pass (re-run). $0, khong OpenAI.

**Ket qua canary:** regularization -> "điều chuẩn" PASS (context vote; dang chu y: trong 24 phieu chi 2 phieu match candidate, ca 2 = điều chuẩn, va Gemma KHONG BAO GIO bo phieu "chuẩn hóa" trong ngu canh regularization — tin hieu dung huong nhung mong). population FAIL — giu "quần thể".

**Chan doan (tu log, tung ca mot):** population fail + cac flip rac (row->rows, class->class, feature->biến đặc trưng) deu quy ve 3 loi CO CHE, khong phai loi thiet ke §36.3:
1. **ROUTING lech design:** implementation cho MOI entry qua back-translation (a) truoc, chi tie moi sang (b). §36.3(b) nguyen van giao polysemy-type cho context vote (population la vi du duoc nêu ten). Hau qua do duoc: 9 entry MLP + 6 prelim co reason=audit_polysemy bi QUYET dinh boi cong cu mu ngu canh — dung cong cu sai cho dung loai loi. population: "quần thể"->"population" match (nghia sinh hoc) -> incumbent thang, context vote khong bao gio chay.
2. **Match rule qua long (containment):** feature: "biến đặc trưng"->EN "feature variable" duoc tinh matched_source vi CHUA chuoi "feature" -> candidate te nhat thang. Phai exact casefold/lemma, cam containment.
3. **Identity-candidate bias:** candidate giu-nguyen-tieng-Anh back-translate ra chinh no (class->class, rows->rows) -> auto-match, auto-thang (a); trong khi candidate VI da nghia thua vi Gemma mu ngu canh dich sang nghia khac (hàng->goods, lớp->layer). (a) thien vi he thong cho source-echo tren term polysemy.
Phu: 2 flip context-vote thang bang 1 phieu match duy nhat (support, pattern) — bau bang 1 tieng noi.

**Flip review (liet ke du 15, doi chieu gold khi co):** DUONG: training ''->huấn luyện (dien cho trong — gia tri thuc), regularization (gold). RUI RO/NGUOC GOLD: layer tầng->lớp (gold tầng; 30/30 phieu Gemma — prior model nguoc style guide, dung lop §9 convention, khong phai loi co che), feature (bug #2), row/class (bug #3), support/pattern (1-phieu). CHURN style: batch/error/estimate/proportion/independence/conditional-independence/product-rule.

**Phan quyet:** dong y CodeX — round-1 la KET QUA PROBE hop le (co che + gate + log van hanh dung), notebook_reelected KHONG promote. Validation §36 CHUA dat. Fix đề xuat (cho user chot truoc khi round-2): F1 polysemy->route thang (b); F2 exact-match; F3 identity candidate khong duoc thang bang (a), chi duoc bau qua (b); F4 nguong toi thieu context-vote: challenger can >=2 phieu match VA > phieu cua incumbent, khong dat -> unresolved_tie giu incumbent (support/pattern het flip 1-phieu; regularization 2-0 van PASS). F1-F3 la sua ve dung design text + tien le §29 keep-source-gate; F4 la nguong moi (thua nhan: chon sau khi thay data — ghi ro trong record nay de khong tu lua).
