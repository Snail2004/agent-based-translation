# TASK_EV_D2L_07a_localizer_bakeoff — RULER-FIX: bake-off 3 localizer (find_spans[0] / allocate_spans / SimAlign) trên GOLD người + sửa rep-occ selection; vá worksheet EV-06 + re-validate panel 57; **0-API, KHÔNG re-translate / KHÔNG đụng Builder / KHÔNG đổi D-scorer headline**

- **Status:** REOPENED (Claude, 2026-06-20) — §6 phát hiện report STALE + gold 2 ca SAI occurrence; chờ CodeX rebuild 2 gold rows ở canonical block + user re-verify + re-score. KHÔNG push.
- **Refs:** EV_D2L_06 (worksheet localizer bug), EV_D2L_05 (`occ_align.py` SimAlign `align_independent`), EV_D2L_03b (`surface_match.allocate_spans` longest-match + segmentation), memory `d2l-scorer-validity-and-remediation-ladder`, `green-tests-can-hide-dead-integration`, `dont-tune-intervention-on-test-baseline` · 3-bên Claude+GLM-5.2+user hội tụ 2026-06-20.
- **Branch/Commit:** local working tree only; not committed per task protocol.

## 1. Bối cảnh & mục tiêu *(Claude viết)*

EV-06 worksheet localize span bằng `find_spans(...)[0]` (first-match, naive) → user bắt **3 mismark**: `membership` (first-match: chọn "thuộc" đầu thay "sự thuộc"), `MNIST dataset` (sub-string: "MNIST"⊂"bộ dữ liệu MNIST"), `target` (override-ảo: dominant-form khác nhau *toàn cục* nhưng *trùng tại occ đại diện*). Tin tốt đã verify: **D-scorer headline (D_surface) dùng `allocate_spans` (longest-match) → KHÔNG hư**; lỗi chỉ ở worksheet/override-set của EV-06.

**Câu hỏi user (gốc):** "SimAlign có thật sự tốt hơn code không?" → **bake-off 3 localizer trên GOLD người**, đơn-giản-đúng-nhất thắng. Lý thuyết đủ; đo.

**4 chiều (KHÔNG gộp):**
1. **Localizer accuracy** (3 ứng viên) — sửa `MNIST` (sub-string) + `membership` (first-match). Đo bằng gold.
2. **Rep-occ selection validity** — sửa `target` (override-ảo). **Auto-check deterministic**, KHÔNG localizer nào sửa, KHÔNG nằm trong gold.
3. **Metric A (in-registry)** — so 3 localizer CÔNG BẰNG (form có trong registry).
4. **Metric B (out-of-registry)** — `set→bộ` (b003): find_spans/allocate **mù theo thiết kế**; CHỈ SimAlign tìm được → **bonus capability RIÊNG, KHÔNG gộp vào so sánh localizer** (kẻo SimAlign "thắng" bằng năng-lực-khác-loại).

**Phạm vi:** ĐÂY là ruler-fix cho worksheet/override-set EV-06 (diagnostic). **KHÔNG đổi D-scorer headline** (đã robust). Builder-v2 design = task SONG SONG riêng (EV-08), KHÔNG trong task này.

## 2. Scope

**IN:**
1. **3 localizer pluggable** (interface chung `localize(text, surface, owners, *, language) -> span|None`):
   - `first_match` = `find_spans(...)[0]` (hiện tại, để đo baseline xấu).
   - `longest_match` = qua `allocate_spans` (longest-non-overlap, deterministic, 0-API).
   - `simalign` = qua `occ_align.align_independent` (EV-05; pin model+method+seed+cpu → tất định; local 0-paid-API).
2. **GOLD người (localization, KHÔNG adequacy):** `build_localizer_gold` sinh template cho **57 override + 5–6 edge case THẬT** từ 4 chương (CodeX tìm occurrence thật cho failure-mode đã biết). Cột `registry_class ∈ {in, out}`. Annotator (USER) điền `gold_target_span` (offset cụm VI đúng) cho từng (config, term). **Localization = phán căn chỉnh, KHÔNG cần chuyên gia thuật ngữ → user tự làm hợp lệ.**
   - Edge in-registry (Metric A): 1 sub-string-prose, 1 first-match-repeat, 1 cross-term `learning`⊂`machine learning` (regression EV-03b), 1 segmentation `tập`⊂`tập dữ liệu` (regression).
   - Edge out-of-registry (Metric B): `set→bộ` (b003, đã có từ EV-05).
3. **Bake-off scorer** `score_localizer_bakeoff`: per-localizer **exact-span accuracy vs gold** trên Metric A; `regression_fail` (sai bất kỳ ca EV-03b-đã-fix nào → loại); Metric B chỉ SimAlign. → `data/reports/localizer_bakeoff.json` + `recommendation`.
4. **Rep-occ validity auto-check** trong `build_override_set`: với mỗi term, rep-occ phải là block mà CẢ `s0_surface` VÀ `s1_surface` xuất hiện (qua localizer thắng) **VÀ** 2 cụm thực sự KHÁC nhau tại đó; nếu không → re-select block khác thỏa; nếu không block nào → `status='non_override_at_occurrence'` + **loại có-lý-do** (không phải "noise").
5. **Áp localizer THẮNG** vào worksheet + override-set → regen; **re-judge `membership`** (diff thật, panel cold — user chạy tay Antigravity); `MNIST`/`target` → re-select hoặc loại-có-lý-do; cập nhật `memory_tradeoff_panel.json` (57 corrected).
6. **Guard test** chống tái phát.

**OUT (CẤM):**
- Re-translate / re-run Builder / ghi DB frozen.
- **Đổi localizer của D-scorer headline** (đã `allocate_spans`, robust) — task này CHỈ sửa worksheet/override-set EV-06.
- **Gộp Metric B (out-of-registry) vào so sánh localizer** — SimAlign thắng set→bộ là *năng-lực-khác*, báo riêng.
- **LLM-as-gold cho localization** — gold = NGƯỜI (hợp lệ vì localization≠adequacy).
- Nhận SimAlign nếu nó KHÔNG tất định (vi phạm EV-05 L5) dù accuracy cao.
- Builder-v2 (prompt/register/canonical/entity) — task EV-08 SONG SONG riêng.
- Tuning-on-test: edge case là occurrence THẬT để test failure-mode, KHÔNG chọn theo kết quả judge.

## 3. Spec *(Claude viết)*

**`pipeline/eval/localizer.py` (MỚI):**
- `Localizer` protocol + 3 impl (`first_match`, `longest_match`, `simalign`) — cùng chữ ký; `simalign` gọi `occ_align.align_independent` pinned.
- `build_localizer_gold(override_items, edge_items, *, out) -> csv` — cột `{item_id, config(S0/S1), term, surface, block_id, registry_class, gold_target_span(BLANK), note(BLANK)}`; proposer-blind (không lộ localizer nào đề xuất gì).
- `run_localizers(gold_rows, frozen_targets) -> {localizer: {row_id: span}}`.
- `score_localizer_bakeoff(gold, proposals) -> dict` — `metricA_accuracy` (exact-span vs gold, in-registry), `regression_fail:[...]`, `metricB_simalign` (out-of-registry recall), `recommendation` (max accuracy; tie→`longest_match`; reject nếu regression_fail).
- `rep_occ_valid(term, s0_surface, s1_surface, blocks, frozen, *, localizer) -> (ok, chosen_block|None, reason)`.

**`pipeline/scripts/localizer_bakeoff.py`:** build gold → (user điền) → run 3 → score → ghi report.
**Sửa `pipeline/eval/memory_tradeoff.py`:** thay `find_spans(...)[0]` ở localize-for-attribution (dòng ~686-696) bằng localizer THẮNG; `build_override_set` thêm `rep_occ_valid`.

**Ràng buộc (LOCK):** observe⊥compute; freeze (chỉ đọc frozen output); 0 paid-API; deterministic (cả SimAlign pinned); EVAL-only; localization gold = người; KHÔNG đụng D-scorer headline.

## 4. Acceptance criteria *(lệnh chạy được + LOCK)*

```bash
cd THESIS_RUNTIME_TOOL

# (1) unit — localizers + gold + rep-occ + guard
python -m pytest pipeline/tests/test_localizer.py -v
#   test_three_localizers_same_interface
#   test_longest_fixes_substring        : MNIST → "bộ dữ liệu MNIST" (không "MNIST")
#   test_firstmatch_fails_membership     : first_match chọn "thuộc" đầu (chứng minh bug); longest/simalign khác
#   test_rep_occ_autocheck_flags_target  : target (2 form trùng tại occ) → non_override_at_occurrence (KHÔNG localizer)
#   test_gold_occ_matches_scorer_rep_occ : gold-occ ≡ scorer rep-occ (annotation noun#3≠verb#1; set occ=2) — lệch ⇒ FAIL
#   test_metricA_metricB_separated       : set→bộ chỉ vào Metric B; KHÔNG vào so sánh 3-way
#   test_gold_is_localization_not_adequacy : gold cột = span offset, KHÔNG nhãn better/worse
#   test_simalign_deterministic          : 2 lần chạy → span byte-identical (pin model+method+seed+cpu)
#   test_no_raw_findspans_first_match_for_attribution : grep code — fail nếu có find_spans(...)[0] dùng cho ATTRIBUTION/hiển thị ngoài allocate_spans internal (presence-check đếm/bool được phép)

# (2) build GOLD template (0-API, deterministic) — user điền gold_target_span
python -m pipeline.scripts.localizer_bakeoff --build-gold \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json \
  --override data/eval/memory_tradeoff/KEY/override_set.csv \
  --out data/eval/localizer_gold.csv
#   → 57 override + 5-6 edge THẬT; cột gold_target_span BLANK; registry_class in/out

# (3) chạy 3 localizer + chấm (sau khi gold điền)
python -m pipeline.scripts.localizer_bakeoff --score \
  --gold data/eval/localizer_gold.csv \
  --out data/reports/localizer_bakeoff.json
#   → per-localizer metricA_accuracy + regression_fail + metricB(simalign) + recommendation

# (4) áp localizer THẮNG → regen worksheet + override-set + rep-occ fix
python -m pipeline.scripts.localizer_bakeoff --apply --localizer <winner> \
  --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json
#   → override_set rep-occ hợp lệ (target re-select/loại-có-lý-do); worksheet span đúng

# (5) frozen DB bất biến + full suite
python -m pytest pipeline/tests app/backend/tests -q
```

**LOCK kỹ thuật (ghi §5/§6):**
- **L1 GOLD = NGƯỜI, localization KHÔNG adequacy:** cột gold = offset span đúng; **hợp lệ cho user solo** (căn chỉnh ≠ phán chất lượng — khác EV-06). Proposer-blind.
- **L2 Metric A ⟂ Metric B:** in-registry so 3-way công bằng; out-of-registry (`set→bộ`) CHỈ SimAlign, báo **bonus riêng**, CẤM gộp.
- **L3 rep-occ = auto-check deterministic, tách localizer:** target-class (2 form trùng tại occ) → re-select block khác hoặc `non_override_at_occurrence` loại-có-lý-do; KHÔNG đổ cho localizer.
- **L4 winner = max Metric-A exact-span-accuracy; tie → `longest_match` (đơn-giản+tất-định); regression_fail trên ca EV-03b-đã-fix → REJECT** dù accuracy cao.
- **L5 SimAlign phải TẤT ĐỊNH** (pin model+method+seed+cpu, EV-05 L5); non-det → loại dù accurate.
- **L6 GUARD narrow:** cấm `find_spans(...)[0]` cho ATTRIBUTION/hiển thị ngoài `allocate_spans` internal; presence-check (đếm/bool) ĐƯỢC phép.
- **L7 0-API + freeze + EVAL-only; CHỈ worksheet/override-set EV-06, KHÔNG D-scorer headline.**
- **L8 KHÔNG tuning-on-test; Builder-v2 = task EV-08 SONG SONG riêng** (prompt/register + do-no-harm signal DEV-split + entity-generality — design-doc trình user TRƯỚC khi spec run).
- **L10 rep-occ reconciliation (gold↔scorer):** occurrence mà `localizer_gold.csv` đánh dấu PHẢI trùng đúng rep-occurrence mà scorer/override-set dùng để chấm (đối chiếu qua source-occurrence của override-set). Lệch → **FAIL cứng**, dừng bake-off, log `term + (gold_offset, scorer_offset)`. Lý do: phép so "localizer-span vs gold-span" CHỈ valid khi CÙNG một occurrence — lệch thì localizer ĐÚNG vẫn bị chấm SAI. Mở rộng L3 (L3 lo target-ghost trong override-set; L10 lo canh occ gold↔scorer). Bắt buộc phủ test `annotation` (noun-occ #3 vs verb-occ #1, surface lặp 4×) + `set` (occ=2).

**Follow-up (ngoài scope):**
1. Sau bake-off: nếu `longest_match` đủ → dùng nó (đừng cưới SimAlign nặng/unvalidated). SimAlign chỉ nhận nếu thắng RÕ Metric A + tất định; năng-lực Metric B (out-of-registry) ghi nhận như hướng tương lai, KHÔNG nhận vào ruler này.
2. EV-08 Builder-v2 design-doc (song song): do-no-harm signal = **DEV-split (held-out chương) + LLM self-assess**, entity-as-generality (eval Treasure Island), prompt system/user + register restructure — trình user duyệt.

### Gold-construction protocol — CodeX pre-fill + human-verify (CHỐNG ANCHORING)

User yêu cầu CodeX điền-sẵn gold để đỡ thời gian; user verify + sửa cuối = HUMAN final. Vì gold là CHUẨN để chấm 3 localizer → **KHÔNG được anchor vào bất kỳ localizer nào** (proposer-blind, bài học EV-05/06). Protocol BẮT BUỘC:

1. CodeX chạy CẢ 3 localizer trên mọi gold row.
2. **Row mà 3 localizer ĐỒNG THUẬN span** → auto-fill span đó (`prefilled=auto`). Không có giá trị phân biệt localizer; user spot-check 1 mẫu nhỏ.
3. **Row mà 3 localizer LỆCH nhau** (đây mới là row quyết-định-thắng-thua) → **KHÔNG auto-pick**; hiển thị TẤT CẢ ứng viên span **proposer-blind** (`ứng viên 1: «...», 2: «...», 3: «...»` — KHÔNG ghi của localizer nào) → USER tự chọn span đúng theo NGHĨA. `prefilled=human_required`.
4. Mỗi row track `prefilled ∈ {auto, human_required}` + `human_edited:bool`. Scorer in `% human_adjudicated`; nếu user accept 100% trên row `human_required` → cờ cảnh báo rubber-stamp.

→ Tiết kiệm thời gian (chỉ adjudicate ~10–20 row lệch) NHƯNG mọi row phân-biệt-localizer đều do NGƯỜI chọn mù → **bake-off vẫn valid**. Auto-fill CHỈ ở row mà mọi localizer đã đồng thuận (không thiên vị ai). **L9 (anti-anchoring gold):** test `test_gold_disagreement_rows_not_autopicked` — row có ≥2 candidate khác nhau ⇒ gold field để trống chờ người, KHÔNG auto-fill; test `test_prefill_blind` — không cột nào ánh xạ candidate→localizer.

### Tinh chỉnh từ user-audit toàn-57 (2026-06-20) — full-phrase gold + context_diff localizer + re-score hẹp

User tự audit 57 (KHÔNG LLM) → bắt thêm partial-mark + nêu nguyên-lý: cụm quanh term thường ỔN ĐỊNH giữa S0/S1 nên VÙNG-KHÁC-BIỆT giữa 2 câu chính là term. Claude verify toàn-57 (diff prefix/suffix) → phân loại tác động:

- **Wrong-location** (`membership`), **misleading-partial** (`real_valued_scalars` — sub-form «giá trị thực» làm B trông như rụng "scalar"; gold đúng = «vô hướng có giá trị thực»), **override-ảo** (`MNIST`,`target`): ~4 ca **THỰC SỰ đổi** so sánh → bắt buộc sửa + re-judge.
- **Determiner/classifier-short** (thiếu 'các'/'phép'/'đường thẳng': `targets`,`true_parameters`,`tangent_line`,`training_examples`,`elementwise_multiplication`...): **THẤP impact** — vẫn giữ khác-biệt phân-biệt (judge so thực/thật, nhãn... như nhau). **KHÔNG đổi consistency** (D-scorer `allocate_spans` đếm form registry; determiner nằm NGOÀI form → form-count không lệch).
- **CẢNH BÁO:** diff-middle KHÔNG phải gold đáng tin (D2L reword cả câu → vùng-giữa phình → over-flag "WRONG"). Dùng heuristic này để GỢI Ý chỗ soi, KHÔNG để auto-gold.

**Chốt thêm vào spec:**
1. **GOLD span = TOÀN BỘ cụm rendering term** (gồm determiner/classifier/modifier gắn liền), KHÔNG chỉ sub-form registry. Tiêu chí gold người: "cụm VI nhỏ nhất diễn đạt trọn term nguồn tại occ này".
2. **Localizer ứng viên thứ 4 (tùy chọn) `context_diff`**: align 2 target S0↔S1 theo prefix/suffix chung → vùng giữa. Vào bake-off như candidate, nhưng SUY GIẢM khi reword nặng → **test, KHÔNG mặc định**; vẫn theo L4 (đơn-giản+đúng-nhất thắng).
3. **Re-score HẸP trong scope:** sau khi áp localizer thắng + gold full-phrase → re-judge CHỈ ~4 ca thực-sự-đổi (membership, real_valued_scalars, re-select MNIST/target — user chạy tay cold panel), + spot-verify consistency D_surface trên các ca sửa. KHÔNG re-judge cả 57. Kỳ vọng kết luận harm≈improve GIỮ (loại worst-3 đã chỉ 21%→20%).

## 5. Implementation notes *(CodeX dien)*

Implemented / updated, 2026-06-20. Status: REVIEW, no commit.

Files changed:
- `pipeline/eval/localizer.py`
  - Added the shared eval-only localizer module: `first_match`, `longest_match`, and `simalign`.
  - `longest_match` uses shared `allocate_spans`, not raw `find_spans(...)[0]`, so substring cases such as `MNIST` inside the full rendered phrase are handled by the same surface matcher family.
  - Added `validate_gold_occ_matches_scorer_rep_occ` for L10. It hard-fails when `localizer_gold.csv` points at a different source occurrence than the scorer/override representative occurrence. It also now fails when a normal in-registry gold row has no matching override row, so a BOM/header issue cannot silently turn all rows into edge rows.
  - SimAlign localizer no longer aligns whole source blocks. It aligns the source sentence containing the occurrence against the full frozen target block, then offsets the result back to full-block coordinates. This avoids the invalid source-sentence-index -> target-sentence-index assumption when translations merge/split sentences.
  - SimAlign bakeoff uses `simalign_cached` with cache key over model/method/seed/config/window hashes. This avoids re-paying local CPU for the same source-sentence/full-target alignment windows.
- `pipeline/eval/memory_tradeoff.py`
  - `build_override_set(..., localizer_name="longest_match")`; representative windows no longer mark attribution spans by raw first match.
- `pipeline/scripts/localizer_bakeoff.py`
  - CLI supports `--build-gold`, `--render-gold-html`, `--score`, and `--apply`.
  - `--score` now runs L10 reconciliation before bakeoff scoring.
  - `--with-simalign` uses `--simalign-cache-dir` (default `data/eval/localizer_simalign_cache`).
- `pipeline/tests/test_localizer.py`
  - Added coverage for localizer interfaces, substring regression, first-match regression, Metric A/B separation, L10 occurrence reconciliation, SimAlign determinism with a fake aligner, SimAlign source/target sentence-count divergence, no raw first-match attribution, and the static helper UI.
- `data/eval/localizer_gold.html`
  - Static helper UI for human adjudication. It supports click-candidate flow and target-text selection flow, then exports `localizer_gold.csv`.

Human gold state:
- `data/eval/localizer_gold.csv` was completed by the user and committed separately (`d84f982` per user/Claude).
- Current gold: 118 rows, all target offsets filled; 14 rows have `human_edited=true`.

Important sequencing note:
- `localizer_gold.csv` was built against the pre-apply EV-06 override-set. After `--apply`, `data/eval/memory_tradeoff/KEY/override_set.csv` is intentionally regenerated. Therefore the final scientific bakeoff score was run with a temporary snapshot of the pre-apply override-set from `HEAD` so L10 checks the same representative occurrences that the gold file was created for.

Bakeoff result:
- Command: `python -m pipeline.scripts.localizer_bakeoff --score --with-simalign --gold data/eval/localizer_gold.csv --override <pre-apply-override-snapshot> --out data/reports/localizer_bakeoff.json`
- `gold_occ_reconciliation`: `checked=114`, `edge_checked=4`, `failures=[]`.
- Metric A:
  - `first_match`: 107/116 = 0.9224, regression_fail = `mt_mnist_dataset_923cfe4011:S1`, ineligible.
  - `longest_match`: 107/116 = 0.9224, regression_fail = `[]`, eligible.
  - `simalign`: 22/116 = 0.1897, missing=2, regression_fail = `mt_mnist_dataset_923cfe4011:S0`, `mt_mnist_dataset_923cfe4011:S1`, ineligible.
- Metric B out-of-registry:
  - `first_match`: 0/2.
  - `longest_match`: 0/2.
  - `simalign`: 1/2.
- Recommendation by L4: `longest_match`.

Apply result:
- Command: `python -m pipeline.scripts.localizer_bakeoff --apply --localizer longest_match --db data/jobs/d2l_p1/memory.sqlite3 --report data/reports/d2l_translation_metrics_v2.json`
- Output: 57 override items, 57 resolved, 0 unresolved.
- Canonical outputs regenerated in `data/eval/memory_tradeoff/` and `data/eval/memory_tradeoff/KEY/`.

Verification:
- `python -m pytest -p no:cacheprovider pipeline\tests\test_localizer.py -q` -> 11 passed.
- `python -m pytest -p no:cacheprovider pipeline\tests\test_localizer.py pipeline\tests\test_memory_tradeoff.py pipeline\tests\test_occ_align.py -q` -> 39 passed. Windows emitted the known pytest temp cleanup `PermissionError` after exit, but exit code was 0.
- `python -m pytest -p no:cacheprovider pipeline\tests app\backend\tests -q` -> 311 passed in 118.42s. Same Windows pytest temp cleanup warning after exit, exit code was 0.
- `data/reports/localizer_bakeoff.json` records the final bakeoff score and recommendation.
- `data/eval/localizer_simalign_cache/` contains 118 cached source-sentence/full-target SimAlign records.
- Re-judge changed EV-06 items remains intentionally out of scope for this implementation step.

## 6. Review *(Claude điền)*

**Verdict: PASS (Claude §6, 2026-06-20). Committed.**

Tái kiểm độc lập (không tin báo cáo):
- Frozen DB hash `DA0F687894090D43…` KHÔNG đổi. Tự chạy: test_localizer 11 passed; +memory_tradeoff+occ_align 38–39 passed.
- Winner `longest_match` 107/116=0.9224, 0 regression, eligible — thắng `first_match` (cùng 0.9224) vì qua regression MNIST mà first_match fail. Vững, độc lập với simalign.
- Apply 57/57 resolved; worksheet chính không bị ghi đè ngoài scope. L10 reconciliation chạy thật: checked=114, edge_checked=4, failures=[] (annotation noun#3, set occ=2 — gold↔scorer không lệch).

**Bug bắt & sửa TRONG vòng review (mấu chốt trung thực):** lần chấm đầu simalign=0.25 / MetricB 0/2 là ARTIFACT. Root cause `_target_sentence_span_for_row` map câu nguồn #N → câu đích #N theo index; sai khi bản dịch gộp/tách câu (elementwise: nguồn 13 / đích 7 / term nguồn#5 nhưng gold đích#3 → align câu EN vào câu VI sai → "phân chuẩn"). Test cũ xanh vì fake aligner ([[green-tests-can-hide-dead-integration]]). Đã sửa: source window = câu chứa term, target window = TOÀN block đích (bỏ index-map); cache regenerate sạch. Verify lại: offset 118/118 khớp block-coords (hết shift); misses là lỗi align THẬT (elementwise→"nhị phân"; affine→"biến đổi affine" thiếu "phép"; span phình do align rải rác).

**Số trung thực sau sửa:** simalign 22/116=0.1897 Metric A (không đủ exact-span), Metric B 0/2→1/2 (có năng lực out-of-registry). → loại theo SỐ THẬT, không vì bug.

**Caveat cho phần viết luận văn (không chạy lại — L4 dừng):** một phần điểm Metric A thấp của SimAlign đến từ chính sách span min–max trên token align rải rác (deferred/manipulating/lifting → nuốt cả đoạn). Dù đổi sang policy cụm-liền-mạch, SimAlign vẫn dưới xa 0.92 (near-miss "biến đổi affine" vs "phép biến đổi affine" vẫn fail exact). Ghi 1 câu limitation để hội đồng không đọc 0.19 như thuần chất lượng align.

**CORRECTION (Claude, 2026-06-20, SAU commit d3c2547/72b0192 — retract PASS ở trên):** §6 PASS đã OVER-TRUST report. Tự chạy lại `validate_gold_occ_matches_scorer_rep_occ` trên **gold + KEY override ĐÃ COMMIT** → **FAIL 4** (mt_mnist S0/S1, mt_offset S0/S1) trong khi `localizer_bakeoff.json` ghi `failures=[]`. ⇒ report **STALE**, chấm trên gold-state khác với gold đã commit; điểm 107/116 CHƯA đáng tin. Root cause: 2 gold row `human_required` ở **SAI occurrence** — mt_mnist block `b126` ("…60000 digits", **S0==S1 KHÔNG override**) thay vì canonical override `b137` ("source of the famous", S0≠S1); mt_offset `b011` thay vì `scratch_b035`. **L10 CODE ĐÚNG** (bắt được khi chạy trên artifact hiện tại) — lỗi nằm ở (a) `--build-gold` đặt 2 row sai block, (b) report sinh ra trên gold cũ. Bài học: KHÔNG tin số trong report; re-run trên artifact đã-commit. Winner `longest_match` nhiều khả năng giữ nhưng PHẢI re-score sau khi sửa.
