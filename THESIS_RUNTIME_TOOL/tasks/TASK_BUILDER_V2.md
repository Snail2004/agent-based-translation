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
