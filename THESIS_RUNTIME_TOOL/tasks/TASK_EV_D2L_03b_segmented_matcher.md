# TASK_EV_D2L_03b_segmented_matcher — Shared surface-matcher v2.1 + Vietnamese word segmentation (xóa lớp va-chạm-âm-tiết) + audit-gộp S0+S1 + gate ngưỡng quyết alignment; 0 API, KHÔNG re-translate

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (ss) · (rr)/(qq)/(pp) · memory `d2l-scorer-validity-and-remediation-ladder`, `dont-tune-intervention-on-test-baseline` · EV_D2L_02 (47620f4), EV_D2L_03 (3918a8a)
- **Branch/Commit:** (điền khi imple xong)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

User phát hiện overlay gán "tập" (= set) cho "tập **trung**" (= focus). Tái kiểm DB thật: `set→"tập hợp"`, variants `["tập hợp","tập"]`; chuỗi "tập" khớp word-boundary **321 lần** nhưng chỉ ~61 là "tập hợp", **260 lần nằm trong từ ghép khác** (tập dữ liệu=127, tập trung=27, tập huấn, tập kiểm tra…). **~257/1608 term có dạng dịch là 1 ÂM TIẾT** (tập, học, lớp, bậc, miền…).

**Gốc rễ:** tiếng Việt cách nhau bằng **ÂM TIẾT** không phải **TỪ** ("tập trung" = 1 từ viết thành 2 âm tiết). Word-boundary regex coi mỗi âm tiết là 1 token → **về bản chất SAI cho tiếng Việt**. Subsumption (03b cũ) chỉ sửa được khi cái-chứa LÀ term registry (machine learning ⊃ learning); KHÔNG sửa âm-tiết-trong-từ-thường (tập ⊂ tập trung). Vá từng biến thể = vô tận (đúng nỗi lo "bao giờ xong?").

**4-bên hội tụ (Claude + CodeX + 2 external agent):** (a) đây là lớp bài toán alignment, không phải matching; (b) **bước nền = Vietnamese word segmentation** (F1 ~97.5–98.3%, 0-API) — xóa nguyên nhân ở tầng phương pháp cho CẢ lớp 257 term + tự sửa luôn case learning; (c) **BÁC bắt translator S1 tự emit alignment** (bẩn ablation S0/S1, self-report ≠ ground-truth, vi phạm observe⊥compute); (d) alignment = **trục-2 audit-gated**, không phải headline mặc định.

**Mục tiêu (D_surface_v2.1, 0 API, re-score output cũ):** (1) shared matcher dùng chung scorer+overlay (hình==số bảo đảm cấu trúc); (2) **segment phía đích** → match theo TOKEN-TỪ; (3) **audit-gộp S0+S1** đo residual → **gate ngưỡng** quyết alignment có cần cho headline. KHÔNG re-translate, KHÔNG alignment trong task này.

## 2. Scope

- **IN (0 API):**
  1. Segmenter (pyvi hoặc underthesea — pure Python, **pin version** → deterministic); cache theo block.
  2. `pipeline/eval/surface_match.py` dùng CHUNG: mask non-prose (length-preserving) → **segment_vi** → match needle/form theo **chuỗi token-từ** + longest-non-overlap across owner + honor `case_sensitive` (cờ regex, KHÔNG casefold) + trả `(start,end,surface)` **map về char offset GỐC**.
  3. Scorer `d2l_translate_score.py` + overlay `thesis_overlay.py` **đều import** module này; XÓA bản sao mask/_find_matches trùng lặp.
  4. Eval-overlay cleanup **hạ cấp**: chỉ giữ fix lỗi NGHĨA thật của Builder (calculus→phép tính), KHÔNG dùng chống va-chạm.
  5. **Residual log** `data/eval/d2l_residual_falsepos.csv` — false-positive mới **GHI vào đây, KHÔNG hot-fix lẻ**; audit theo đợt.
  6. Report → `metric_version=d2l_translate_score_v2_1` + provenance `segmentation:{tool,version}`; re-score kèm `--gold-variants`.
  7. **Audit harness** sinh mẫu cho audit-gộp (§4) + tính FP-rate khi nhãn người đã điền.

- **OUT (CẤM):**
  - Alignment / SimAlign / awesome-align / LLM-aligner → `ALIGN_PILOT_01` (task sau, audit-gated).
  - **Bắt translator S1 emit alignment** (bẩn ablation — đã bác 4 bên).
  - RE-TRANSLATE, đổi prompt injection, ghi DB frozen, API.
  - **Hot-fix false-positive case-by-case** — phải qua residual-list + audit đợt.
  - Tuyên bố highlight là "alignment occurrence-level".

## 3. Spec *(Claude viết)*

**`pipeline/eval/surface_match.py` (MỚI, dùng chung):**
- `MASK_PATTERNS`, `mask_non_prose(text)` — 1 bản duy nhất (chuyển từ scorer+overlay về đây).
- `segment_vi(text) -> list[(token, char_start, char_end)]` qua pyvi/underthesea (pin); giữ char span gốc.
- `find_spans(text, needle, *, case_sensitive=False)`: segment text + needle → khớp theo **dãy token-từ liên tiếp** (KHÔNG substring ký tự). "tập trung"→["tập trung"]; needle "tập"→["tập"] ⇒ KHÔNG khớp. "tập hợp"→["tập hợp"]; needle "tập hợp" ⇒ khớp. Offset = char span gốc.
- `allocate_spans(text, owners)`: gom span mọi owner, sort dài→ngắn, cấp phát non-overlap → mỗi span 1 owner (machine learning thắng learning; token "học máy" thắng "học").

**Scorer `d2l_translate_score.py`:** bỏ matcher cục bộ → gọi `surface_match`; `_score_registry_consistency` dùng `allocate_spans` per block (source+target). metric_version `d2l_translate_score_v2_1`.

**Overlay `thesis_overlay.py`:** bỏ bản sao → gọi `surface_match` (cùng `allocate_spans`) → span highlight == span scorer đếm. Giữ scoped per-block/chapter (perf A02).

**Test cấu trúc bắt buộc:** `test_overlay_matches_scorer_forms`.

**Ràng buộc (LOCK):** observe⊥compute; freeze (eval-overlay only, KHÔNG ghi DB); 0 API; offset-preserving; deterministic (pin segmenter).

## 4. Acceptance criteria *(lệnh chạy được + LOCK kỹ thuật)*

```bash
cd THESIS_RUNTIME_TOOL

# (1) segmentation matcher + offset + subsumption
python -m pytest pipeline/tests/test_surface_match.py pipeline/tests/test_d2l_translate_score.py -v
#   test_segment_kills_syllable_collision : "tập"(set) KHÔNG khớp trong "tập trung"/"tập dữ liệu"; khớp "tập hợp"
#   test_segment_subsumes_machine_learning: "học"(learning) KHÔNG khớp trong "học máy"
#   test_offsets_map_to_original          : span trỏ đúng ký tự gốc

# (2) hình==số cấu trúc
python -m pytest app/backend/tests/test_thesis_overlay.py -v   # gồm test_overlay_matches_scorer_forms

# (3) re-score 0 API → v2.1
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --chapters d2l_introduction d2l_preliminaries d2l_linear_networks d2l_multilayer_perceptrons \
  --config S1 --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_translation_metrics_v2.json
# → metric_version=d2l_translate_score_v2_1; set/learning hết false "tập"/"học máy"; in delta hard-headline

# (4) AUDIT HARNESS sinh mẫu (CodeX build harness; LABEL = người)
python -m pipeline.scripts.audit_sample --report data/reports/d2l_translation_metrics_v2.json \
  --tier hard --n 30 --configs S0 S1 --out data/eval/d2l_seg_audit_sample.csv
# → 30 occurrence × 2 config = 60 dòng: source_term, block_id, sentence_EN, sentence_VI,
#   predicted_form(S0/S1), [cột TRỐNG: human_label, note]

# (5) frozen DB bất biến + full suite
python -m pytest pipeline/tests app/backend/tests -q
```

**LOCK kỹ thuật phải tuân (ghi vào §5/§6 khi review):**
- **LOCK-1 IAA neo NGƯỜI:** gold audit = **tác giả hand-label** `human_label ∈ {match, drift, undetected, builder-multi-variant}`. Model CHỈ đề xuất để tăng tốc; nhãn cuối = người. Cohen κ + ngưỡng **≥0.7** CHỈ áp khi có **annotator-2 là người độc lập**; **CẤM** dùng 2 session model gọi là IAA (= self-consistency, sẽ bị hội đồng bác). Báo cáo trung thực "N occurrence hand-audited by author".
- **LOCK-2 audit CẢ S0+S1** (30×2=60): profile false-positive S0 (blind) ≠ S1 (có glossary); in FP-rate **riêng từng config**.
- **LOCK-3 GATE ngưỡng** (ghi quyết định + số vào §6): surface false-positive rate hard-tier sau segmentation →
  - **≤ 5%**: segmentation ĐỦ cho headline; alignment = trục-2 làm sau.
  - **5–15%**: `ALIGN_PILOT_01` cho residual hard-tier.
  - **> 15%**: alignment cần cho headline.
- **LOCK-4 dual-axis (thesis-level):** luận văn báo cáo CẢ headline (segmentation-based) LẪN alignment-axis (kèm error-bar đã-audit) khi ALIGN_PILOT xong — không có kịch bản alignment vắng mặt; gate chỉ quyết cái nào headline.

## 5. Implementation notes *(CodeX điền)*

## 6. Review *(Claude điền — gồm: audit 60 occurrence, FP-rate S0/S1, quyết định GATE)*
