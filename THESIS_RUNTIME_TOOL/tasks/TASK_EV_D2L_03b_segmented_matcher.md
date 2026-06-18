# TASK_EV_D2L_03b_segmented_matcher — Shared surface-matcher **v2.2** + Vietnamese word segmentation (TARGET-only) + audit-gộp S0+S1 + gate; 0 API, KHÔNG re-translate

- **Status:** READY
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

## 6. Review *(Claude điền — audit 60 occ, FP-rate S0/S1, quyết định GATE, perf số)*
