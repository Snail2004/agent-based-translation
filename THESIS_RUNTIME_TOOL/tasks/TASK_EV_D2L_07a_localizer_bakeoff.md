# TASK_EV_D2L_07a_localizer_bakeoff — RULER-FIX: bake-off 3 localizer (find_spans[0] / allocate_spans / SimAlign) trên GOLD người + sửa rep-occ selection; vá worksheet EV-06 + re-validate panel 57; **0-API, KHÔNG re-translate / KHÔNG đụng Builder / KHÔNG đổi D-scorer headline**

- **Status:** READY (Claude, 2026-06-20) — chờ CodeX §5 + REVIEW; KHÔNG commit.
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

**Follow-up (ngoài scope):**
1. Sau bake-off: nếu `longest_match` đủ → dùng nó (đừng cưới SimAlign nặng/unvalidated). SimAlign chỉ nhận nếu thắng RÕ Metric A + tất định; năng-lực Metric B (out-of-registry) ghi nhận như hướng tương lai, KHÔNG nhận vào ruler này.
2. EV-08 Builder-v2 design-doc (song song): do-no-harm signal = **DEV-split (held-out chương) + LLM self-assess**, entity-as-generality (eval Treasure Island), prompt system/user + register restructure — trình user duyệt.

## 5. Implementation notes *(CodeX điền)*

<!-- CodeX: files changed / implemented / deviation / commands / not run. KHÔNG commit. -->

## 6. Review *(Claude điền)*

<!-- Claude: tái kiểm độc lập — frozen hash, tự chạy test, 3 localizer interface==production, Metric A/B tách thật, rep-occ auto-check, guard narrow đúng (presence-check không bị chặn), SimAlign deterministic, winner theo L4. -->
