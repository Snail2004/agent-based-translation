# TASK BUILDER-V2 — Builder D2L v2: trích độc lập (recall) + sổ-tay-có-lọc (memory-pack) + code consolidation là QUYỀN CUỐI

Status: Stage B PASS (Claude §13, re-derived 2026-06-29) → Stage C chờ (priority-budget pack thay greedy-alphabet; chronological notebook thật). Stage A PASS (§6).
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
