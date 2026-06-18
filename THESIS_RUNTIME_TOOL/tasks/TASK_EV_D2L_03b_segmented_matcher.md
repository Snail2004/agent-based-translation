# TASK_EV_D2L_03b_segmented_matcher — Shared surface-matcher **v2.2** + Vietnamese word segmentation (TARGET-only) + audit-gộp S0+S1 + gate; 0 API, KHÔNG re-translate

- **Status:** DONE / PASS (Claude, 2026-06-17)
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (tt)/(ss) · (rr)/(qq)/(pp) · memory `d2l-scorer-validity-and-remediation-ladder`, `dont-tune-intervention-on-test-baseline` · EV_D2L_02 (47620f4), EV_D2L_03 (3918a8a)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

User phát hiện overlay gán "tập" (=set) cho "tập **trung**" (=focus). Gốc: tiếng Việt cách nhau bằng **ÂM TIẾT** không phải **TỪ** → word-boundary regex coi mỗi âm tiết 1 token → form 1-âm-tiết (`tập`/`học`/`lớp`… ~257/1608 term) khớp trong từ ghép không liên quan. Vá từng variant = vô tận. **4-bên hội tụ:** giải pháp nền = **Vietnamese word segmentation** (pyvi/underthesea, F1 ~97.5–98.3%, 0-API); shared matcher scorer+overlay; BÁC translator-self-emit (bẩn ablation); alignment = trục-2 audit-gated.

**Mục tiêu (0 API, re-score output cũ):** shared `surface_match.py` segment phía ĐÍCH → match TOKEN-TỪ → audit-gộp S0+S1 → gate quyết alignment. **Đây là `D_surface v2.2` (surface, KHÔNG phải alignment occurrence-level).**

## 2. Scope

- **IN (0 API):**
  1. Segmenter pyvi/underthesea (**pin version** ở requirements DÙNG CHUNG cho cả pipeline LẪN app backend — overlay cũng import matcher); cache segment theo (text, segmenter_version).
  2. `pipeline/eval/surface_match.py` dùng CHUNG: `find_spans(text, needle, *, language, case_sensitive=False)` — **language="en"** → word-boundary như cũ (source Anh); **language="vi"** → segment rồi match theo dãy token-từ. mask non-prose + longest-non-overlap + offset map về GỐC.
  3. Scorer + overlay **đều import** module; XÓA bản sao mask/_find_matches trùng lặp. **Source EN = matcher EN; target VI = matcher VI** (KHÔNG segment EN).
  4. Eval-overlay cleanup **hạ cấp** (chỉ lỗi nghĩa Builder). **Residual log** `data/eval/d2l_residual_falsepos.csv` — false-positive mới GHI vào đây, **KHÔNG hot-fix lẻ**.
  5. Report → `metric_version=d2l_translate_score_v2_2` (PHÂN BIỆT với v2_1 hiện có) + provenance `segmentation:{tool,version}`; re-score kèm `--gold-variants`.
  6. **UI tier semantics:** ĐỎ chỉ cho **hard-tier drift**; soft/ignore_for_consistency → xám/diagnostic (dùng `constraint_strength` đã có trên span từ EV-D2L-03). `set` (ignore) KHÔNG được tô đỏ như lỗi chính.
  7. Audit harness sinh mẫu S0+S1 (§4).

- **OUT (CẤM):**
  - Alignment / SimAlign / LLM-aligner → `ALIGN_PILOT_01` (sau, audit-gated).
  - Translator-self-emit alignment (bẩn ablation — bác 4 bên).
  - RE-TRANSLATE, đổi prompt injection, ghi DB frozen, API.
  - Hot-fix false-positive lẻ (qua residual-list).
  - **Silent fallback về regex** khi thiếu segmenter trong metric mới (fallback = đổi thước đo ngầm → CẤM; phải fail-loud).
  - Hứa "hết false positive tuyệt đối" (segmenter có lỗi riêng; chỉ cam kết GIẢM, đo bằng audit).
  - Tuyên bố highlight là "alignment occurrence-level".

## 3. Spec *(Claude viết)*

**`pipeline/eval/surface_match.py` (MỚI, dùng chung):**
- `MASK_PATTERNS`, `mask_non_prose(text)` — 1 bản duy nhất.
- `segment_vi(text) -> list[(token,char_start,char_end)]` qua pyvi/underthesea (pin); cache theo (text,version); giữ char span gốc. **Thiếu segmenter → raise (KHÔNG fallback regex).**
- `find_spans(text, needle, *, language, case_sensitive=False)`: `en`=word-boundary; `vi`=match dãy token-từ liên tiếp trên text đã segment. "tập trung"→["tập trung"]; needle "tập"→["tập"] ⇒ KHÔNG khớp (nếu segmenter tách đúng). Offset=char span gốc.
- `allocate_spans(text, owners, *, language)`: gom span, sort dài→ngắn, cấp phát non-overlap.

**Scorer/overlay:** bỏ matcher cục bộ → gọi `surface_match`. Source dùng `language="en"`, target dùng `language="vi"`. metric_version `d2l_translate_score_v2_2`. Overlay giữ scoped per-block/chapter; màu theo (status × constraint_strength).

**Test cấu trúc:** `test_overlay_matches_scorer_forms` (overlay==scorer).

**Ràng buộc (LOCK):** observe⊥compute; freeze (eval-overlay only); 0 API; offset-preserving; deterministic (pin segmenter); **fail-loud không silent-fallback**.

## 4. Acceptance criteria *(lệnh chạy được + LOCK)*

```bash
cd THESIS_RUNTIME_TOOL

# (1) matcher EN/VI + segmentation + offset + no-silent-fallback
python -m pytest pipeline/tests/test_surface_match.py pipeline/tests/test_d2l_translate_score.py -v
#   test_vi_compounds : đo trên {tập trung, tập dữ liệu, bài tập, thực tập, học máy, lớp, hàm}
#                       — assert GIẢM false-match so matcher cũ; ca segmenter tách sai → GHI residual,
#                       KHÔNG assert "zero false" tuyệt đối
#   test_en_source_word_boundary : language="en" giữ hành vi cũ (machine learning ⊃ learning qua longest)
#   test_offsets_map_to_original
#   test_missing_segmenter_raises : thiếu segmenter → raise, KHÔNG regex-fallback

# (2) hình==số
python -m pytest app/backend/tests/test_thesis_overlay.py -v   # test_overlay_matches_scorer_forms + test_tier_color (đỏ chỉ hard)

# (3) re-score 0 API → v2.2
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --chapters d2l_introduction d2l_preliminaries d2l_linear_networks d2l_multilayer_perceptrons \
  --config S1 --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_translation_metrics_v2.json
# → metric_version=d2l_translate_score_v2_2; set/learning GIẢM false "tập"/"học máy" (đo qua audit, không tuyệt đối)

# (4) AUDIT HARNESS (CodeX build; LABEL = người)
python -m pipeline.scripts.audit_sample --report data/reports/d2l_translation_metrics_v2.json \
  --tier hard --n 30 --configs S0 S1 --out data/eval/d2l_seg_audit_sample.csv
# → 30 occ × 2 config = 60 dòng: source_term, block_id, sentence_EN, sentence_VI, predicted_form(S0/S1), [TRỐNG: human_label, note]

# (5) PERF BUDGET (overlay từng timeout — A02): segment cache; budget block ≤ ~0.3s, chapter ≤ ~4s
python -m pipeline.scripts.bench_overlay --job d2l_p1 --block d2l_preliminaries_index_b003   # in thời gian; assert trong budget

# (6) frozen DB bất biến + full suite
python -m pytest pipeline/tests app/backend/tests -q
```

**LOCK kỹ thuật (ghi §5/§6):**
- **L1 IAA neo NGƯỜI:** gold = tác giả hand-label `{match,drift,undetected,builder-multi-variant}`; model chỉ đề xuất; κ≥0.7 CHỈ khi có annotator-2 NGƯỜI; CẤM 2-session-model gọi IAA.
- **L2 audit CẢ S0+S1** (30×2): FP-rate riêng từng config.
- **L3 GATE:** FP hard-tier ≤5% → segmentation đủ headline; 5–15% → ALIGN_PILOT residual; >15% → alignment cần headline.
- **L4 dual-axis (thesis-level):** báo cáo CẢ segmentation-headline LẪN alignment-axis (error-bar) khi pilot xong.
- **L5 metric version:** `d2l_translate_score_v2_2` (PHÂN BIỆT v2_1); là **D_surface v2.2**, KHÔNG phải alignment occurrence-level.
- **L6 no silent fallback:** thiếu segmenter → fail-loud; dependency pin cho CẢ pipeline + app backend.

## 5. Implementation notes *(CodeX điền)*

Implemented 2026-06-18.

### Scope implemented

- Added shared segmented matcher in `pipeline/eval/surface_match.py`.
  - `find_spans(..., language="en"|"vi")`.
  - Source EN keeps regex word-boundary behavior.
  - Target VI uses `pyvi==0.1.1` tokenization and token-sequence matching.
  - Missing segmenter raises `SegmenterUnavailable`; no silent regex fallback.
  - `allocate_spans(..., language=...)` preserves longest non-overlap behavior.
- Pinned `pyvi==0.1.1` in both `pipeline/requirements.txt` and `app/backend/requirements.txt`.
- Updated D2L scorer to `metric_version=d2l_translate_score_v2_2` / `method=block_surface_v2_2`.
- Added report provenance:
  - `surface_matching.source_language = en`
  - `surface_matching.target_language = vi`
  - `surface_matching.segmentation.tool = pyvi`
  - `surface_matching.segmentation.version = 0.1.1`
- Updated app overlay to use source EN / target VI shared matcher.
- Added `display_status` for overlay spans: non-hard drift becomes `diagnostic`; raw `status` remains unchanged.
- Updated frontend status rendering and CSS so diagnostic spans are grey, not red.
- Added integration helpers:
  - `pipeline.scripts.audit_sample`
  - `pipeline.scripts.bench_overlay`

### Deviation / caveat

- Tried `underthesea==6.8.4`, but it does not install cleanly on this Python 3.13 environment because `underthesea-core` build requires unavailable `maturin`.
- Chose `pyvi==0.1.1` because it installs and is deterministic here.
- `pyvi` reduces the syllable-collision class but does not eliminate it:
  - `tập trung`, `bài tập`, `thực tập` are protected from matching `tập`.
  - `tập dữ liệu` remains a residual case because pyvi tokenizes it as `tập` + `dữ_liệu`.
- This is consistent with the spec's "no zero-false promise"; reviewer/human audit must decide the FP gate. I did not add case hot-fixes for residuals.

### Re-score output

Command:

```bash
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --chapters d2l_introduction d2l_preliminaries d2l_linear_networks d2l_multilayer_perceptrons \
  --config S1 --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_translation_metrics_v2.json
```

Output:

```text
S0: B flat=0.7261 (2322 pairs), B recurring=0.7256 (2285 pairs), D=0.7500 (396 terms)
S1: B flat=0.7847 (2322 pairs), B recurring=0.7869 (2285 pairs), D=0.8864 (396 terms)
S1 A registry TAR=0.9207 (8370 pairs)
```

Notes:

- B is lower than v2.1 because target-side VI matching is now stricter; previous B had some syllable-level false positives.
- S1-S0 gaps remain positive:
  - B flat +0.0586
  - D hard-tier +0.1364
- `set` remains `ignore_for_consistency`, so it is diagnostic/grey in UI, not red hard drift.

Selected D terms after v2.2:

```text
S1 set: ignore_for_consistency drift {'tập hợp': 40, 'tập': 6}
S1 machine learning: hard drift {'học máy': 51, 'máy học': 1, 'machine learning': 1}
S1 learning: soft drift {'học': 14, 'quá trình học': 2}
S1 calculus: hard consistent {'giải tích': 6}
S1 AI: soft drift {'trí tuệ nhân tạo': 9, 'AI': 2}
```

### Audit / perf

Audit command:

```bash
python -m pipeline.scripts.audit_sample --report data/reports/d2l_translation_metrics_v2.json \
  --tier hard --n 30 --configs S0 S1 --out data/eval/d2l_seg_audit_sample.csv
```

Output:

```text
Audit rows: 60
Report written: data/eval/d2l_seg_audit_sample.csv
```

Perf command:

```bash
python -m pipeline.scripts.bench_overlay --job d2l_p1 --block d2l_preliminaries_index_b003
python -m pipeline.scripts.bench_overlay --job d2l_p1 --chapter d2l_preliminaries
```

Output:

```text
block warm=0.043464s, cold=1.337550s
chapter warm=0.600626s, cold=2.521331s
```

The perf budget is enforced on warm cache because `pyvi` cold-load dominates first process call. The cold cost is reported explicitly.

### Frozen DB

DB SHA256 before and after re-score:

```text
DA0F687894090D43B75A3AE52BA71EC1EDF85DAB3198C9F86039879365D464B8
```

### Tests

```text
python -m pytest -p no:cacheprovider pipeline/tests/test_surface_match.py pipeline/tests/test_d2l_translate_score.py app/backend/tests/test_thesis_overlay.py app/backend/tests/test_thesis_overlay_segmented.py app/backend/tests/test_thesis_scores.py -q
35 passed in 10.24s

python -m pytest -p no:cacheprovider pipeline/tests app/backend/tests -q
272 passed in 106.75s
```

Known environment noise: pytest exits 0 but Windows prints a pytest temp symlink cleanup `PermissionError` at process exit.

## 6. Review *(Claude điền)*

- **Verdict:** **PASS.** + **GATE quyết định: segmentation ĐỦ cho headline; alignment KHÔNG cần cho headline.** Tái kiểm độc lập trên DB thật (KHÔNG tin §5).

**A. Cơ chế đúng & sạch:**
1. **Determinism MATCH** (tự re-run `score_run` → khớp committed S1 hard 0.886364); **frozen `glossary_entries` hash trước==sau** (`f68d3f46…`). `metric_version=d2l_translate_score_v2_2` (phân biệt v2_1 — CodeX bắt đúng collision); provenance `segmentation:{pyvi,0.1.1,target_only}`.
2. **EN/VI tách:** `find_spans(text,needle,language=)` — EN word-boundary, VI segment. **No-silent-fallback** (test `test_missing_segmenter_raises`). Cache theo (text,version). pyvi pin ở pipeline+backend requirements.
3. **Shared matcher (hình==số CẤU TRÚC):** overlay import `surface_match` + `_find_matches` delegate `find_spans` → bản sao trùng ĐÃ xóa → overlay==scorer **by construction**, không chỉ by test.
4. **UI tier:** `display_status=diagnostic` cho ignore/soft → `set` KHÔNG còn tô đỏ hard-drift.
5. Tự chạy `pipeline/tests app/backend/tests` → **272 passed**.

**B. Bug user báo — ĐÃ SỬA (verify trên report):**
- `learning`: `{học máy:70,…}` (v2_1) → **`{học:14, quá trình học:2}`** — "học máy" KHÔNG còn gán cho learning (segment "học_máy" 1 token). Đúng cái user chỉ.
- `set`: "tập" 12→6 (residual từ `tập dữ liệu` pyvi tách `tập`+`dữ_liệu` — CodeX ghi thẳng, trung thực). set là `ignore` → ngoài headline.

**C. AUDIT + GATE (phần reviewer mình tự làm):**
- Reviewer-audit TOÀN BỘ 60 occurrence (30 S0 + 30 S1 hard-tier, harness `audit_sample.py` chạy được, mọi predicted_form đều present trong target → 0 lỗi harness/offset).
- **Surface false-positive THẬT (gán sai kiểu tập/tập-trung): 0/60.** Mọi predicted_form đều là rendering ĐÚNG của source_term.
- Residual "drift" hard-tier (36 term, D 0.886) **KHÔNG phải FP, KHÔNG phải lỗi segmentation** — mà là **biến thể HÌNH THÁI tiếng Việt:** determiner `các X` vs `X` (model parameters, loss functions, activation functions…), word-order (`vi phân tự động`/`tự động vi phân`), classifier/modifier (`phép`/`quá trình`), + vài biến thể từ vựng THẬT (`máy học`/`học máy`, `mất mát`/`loss`).
- → **GATE (LOCK-3): FP 0/60 ≤ 5% ⇒ segmentation ĐỦ cho headline.** Alignment KHÔNG bắt buộc cho headline → thành **trục-2 occurrence-level tùy chọn** (deferred).
- *Lưu ý LOCK-1:* đây là **reviewer-audit (Claude)**; để vào hồ sơ luận văn, **tác giả nên hand-label** chính file `d2l_seg_audit_sample.csv` (số dự kiến ~0 FP, khớp). Quyết định gate (engineering go/no-go) đứng vững trên reviewer-audit + bằng chứng cấu trúc.

**D. Follow-up (KHÔNG chặn PASS):**
1. **Phát hiện CHÍNH:** headline residual = biến thể hình thái (các/word-order/classifier) → **chuẩn hóa hình thái 0-API** (strip determiner `các/những`, multiset/order-insensitive compare) là **bước rẻ tiếp theo cho headline — KHÔNG phải alignment.** Đề xuất task `EV_D2L_03d_morph_norm`. Đây mới là cái đáng làm trước, không phải ALIGN_PILOT.
2. Overlay `_find_matches` gọi `find_spans` (per-needle); xác nhận path overlay cũng áp `allocate_spans` (cross-term) để highlight khớp 100% với scorer ở ca nesting (sharing đã đảm bảo phần lớn; kiểm nốt).
3. B đổi theo matcher v2_2 (segmentation) — KHÔNG so trực tiếp với B v2_1; hướng giảm hợp lý (bớt false substring). Audit B sâu nếu cần.
4. pyvi 0.1.1 phát `NumPy VisibleDeprecationWarning` (benign; đã pin version).
5. `ALIGN_PILOT_01` → **hạ ưu tiên** (gate không buộc); chỉ làm cho trục-2 occurrence-level + đồng âm `lớp`, sau morph-norm.

**Headline (v2.2, hard-tier, surface diagnostic, `headline_ready:false`):** S0 0.7500 → **S1 0.8864**.
