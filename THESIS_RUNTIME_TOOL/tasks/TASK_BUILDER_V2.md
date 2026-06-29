# TASK BUILDER-V2 — Builder D2L v2: trích độc lập (recall) + sổ-tay-có-lọc (memory-pack) + code consolidation là QUYỀN CUỐI

Status: Stage A+B+C1 PASS (Claude §6/§13/§16) → **Stage C2 spec READY + SIẾT theo CodeX (§17 + §17.8, 6 điểm đã verify)** chờ CodeX implement §5: driver online + pack-từ-notebook-v2-mọi-surface + cache-DB-riêng + config-v7-verbatim + cost-gate (`--estimate-only` rồi STOP, KHÔNG gọi API). C tách C1(DONE)→C2(API pilot, user duyệt $ TRƯỚC).
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
- **Cache key — guard (CodeX #2, Claude verify sâu hơn):** Claude đọc `llm_client.py`: key thật = `hash(messages, temperature, seed, reasoning_effort, response_format)` — **THIẾU cả `model`, `max_output_tokens`, `verbosity`** (CodeX nói có `model` là nhầm; thực ra cũng thiếu). **Cách xử lý C2 (blast-radius = 0):** dùng **cache DB MỚI TINH + 1 config CỐ ĐỊNH** cho cả run ⇒ mọi key do chính C2 ghi dưới đúng 1 config ⇒ stale-hit BẤT KHẢ, các field thiếu thành vô hại. Ghi **full config vào artifact + `tag`** để kiểm. *(Sửa cache-key chung trong `LLMClient` là việc RIÊNG, có hệ luỵ: đổi key ⇒ vô hiệu TOÀN BỘ cache production hiện có → KHÔNG gộp vào C2; ghi follow-up.)*
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
| 2 | Cache key thiếu `max_output_tokens`/`verbosity` | **NHẬN + verify sâu hơn:** đọc `llm_client.py` → key = `hash(messages,temp,seed,reasoning,response_format)`, **thiếu cả `model`** (CodeX nói có model là nhầm). Xử lý C2: cache MỚI + 1 config CỐ ĐỊNH ⇒ stale-hit bất khả, field thiếu vô hại; sửa key chung = follow-up riêng (đổi key vô hiệu cache production). → 17.4 |
| 3 | "Same model v7" chưa đủ nếu đổi decoding | **NHẬN, sửa MẠNH hơn:** verify `llm_prepass.yaml` = gpt-5.4-mini/temp1.0/**seed20260612**/reasoning-none/verbosity-low/max6144. → **dùng NGUYÊN config v7** (bỏ temp=0), decoding hết là confound; tái lập = seed + CACHE. C2 = so cấp-hệ-thống, không claim ablation. → 17.3 |
| 4 | Estimate-only không được dùng notebook rỗng | **NHẬN.** Estimate = upper-bound bảo thủ từ C1 (141k prompt + output cap) × pricing thật → $0.155 thực / $0.65 trần. → 17.4 |
| 5 | Adapter phải match mọi `source_variants[].surface` | **NHẬN.** Pack quét toàn bộ surface đã lưu, không chỉ canonical+number-variant. → 17.2 |
| 6 | Parse failure không được "skip coi như xong" | **NHẬN.** re-ask 1 lần → `parse_failure_count` → `status=degraded` nếu >0 → run degraded chỉ debug, không rút kết luận chất lượng. → 17.1 |

**Acceptance bổ sung:** test cache-hit chạy trên **cache DB riêng** (xác nhận KHÔNG đụng frozen); test `--estimate-only` ra số > 0 dù notebook rỗng (dùng upper-bound); test `degraded` set khi mock 1 parse-fail.

**Quy trình:** CodeX điền §5 implementation + chạy `--estimate-only` ghi số → **STOP, KHÔNG gọi API thật, KHÔNG commit**. Claude review §6 + **trình $ cho user duyệt** → user OK mới chạy `--confirm-usd`.
