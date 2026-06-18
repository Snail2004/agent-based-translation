# TASK_EV_D2L_03b_cross_term_subsumption — Shared surface-matcher v2.1 (scorer+overlay dùng CHUNG) + cross-term subsumption (longest-source/target across terms) + eval-overlay variant cleanup; 0 API, KHÔNG re-translate

- **Status:** REVIEW
- **Refs:** THESIS_ARCHITECTURE_LOCK §10 (rr) · (pp)/(qq) thang remediation · memory `d2l-scorer-validity-and-remediation-ladder`, `dont-tune-intervention-on-test-baseline` · EV_D2L_02 (47620f4), EV_D2L_03 (3918a8a)

## 1. Bối cảnh & mục tiêu *(Claude viết)*

User phát hiện overlay hiện "học máy" (= machine learning) như bản dịch của **learning**. Tái kiểm 3 bên trên DB thật:
- **Pollution:** `learning.allowed_variants = ["học","quá trình học","học máy"]` → "học máy" bị Builder liệt nhầm là biến thể của learning (cùng họ calculus⊃phép tính).
- **Containment:** "learning" khớp word-boundary BÊN TRONG "machine learning"/"deep learning"; **202/330 (61%) occurrence "learning" thực ra nằm trong ML/DL**. Không phải ca lẻ.
- **2 matcher TÁCH RỜI đang phân kỳ:** `pipeline/eval/d2l_translate_score.py` và `app/backend/services/thesis_overlay.py` MỖI nơi có bản sao `_MASK_PATTERNS`/`_mask_non_prose`/`_find_matches`. Overlay `_find_matches` dùng `IGNORECASE` + **KHÔNG longest-non-overlap** → overlay **over-highlight** form lồng/chứa mà scorer (EV-D2L-02) đã triệt ⇒ **hình≠số đang VỠ cho nesting/containment**, không chỉ ca learning.

EV-D2L-03 đã bảo vệ CON SỐ (learning=soft, ngoài hard headline) nhưng tầng **hiển thị + occurrence-attribution** vẫn sai. Đây là vấn đề surface-match (đúng như đã dự báo), nhưng **chưa cần alignment** (Cấp 3) — bước đúng là 0-API deterministic.

**Mục tiêu (D_surface_v2.1, 0 API, re-score output cũ):** (1) tách **matcher dùng CHUNG** cho scorer + overlay (1 nguồn sự thật → hình==số có bảo đảm cấu trúc); (2) **cross-term subsumption**: cấp phát span theo block, **longest-first across MỌI term** (machine learning thắng learning ở nguồn; "học máy" thắng "học" ở đích); (3) **eval-overlay cleanup** biến thể ô nhiễm (learning⊅"học máy", …). Phải xử lý **CẢ pollution lẫn containment** — thiếu một trong hai là chưa đủ.

## 2. Scope

- **IN (0 API):**
  1. Module dùng chung `pipeline/eval/surface_match.py`: mask non-prose (length-preserving) + normalize (apostrophe, length-preserving) + match theo word-boundary, **honor `case_sensitive` bằng cờ regex (KHÔNG casefold text → GIỮ offset cho overlay)** + trả `(start,end,surface)` map về text GỐC.
  2. **`allocate_spans` (joint, theo block):** gom mọi candidate span across tất cả owner (term/form), sort **độ dài giảm dần**, cấp phát **non-overlap** → mỗi span thuộc đúng 1 owner. Dùng cho CẢ source (owner=term) lẫn target (owner=term-form).
  3. Scorer `d2l_translate_score.py` + overlay `thesis_overlay.py` **đều import** module này; XÓA bản sao trùng lặp.
  4. Eval-overlay: thêm `d2l_glossary_fixes.csv` `learning,remove_variant,học máy` (+ rà các biến thể chứa-nhau khác: deep learning⊅"học máy sâu" nếu có, …) — mỗi dòng justification linguistic.
  5. Report → `metric_version=d2l_translate_score_v2_1`, ghi rõ vẫn là surface (alignment=false). Re-score (kèm `--gold-variants`).

- **OUT (CẤM):**
  - Alignment / SimAlign / awesome-align → `ALIGN_PILOT_01` (Cấp 3).
  - RE-TRANSLATE, đổi prompt injection, ghi DB frozen, API.
  - Tuyên bố highlight là "liên kết dịch occurrence-level chính xác" — chỉ được nói "term nguồn xuất hiện + form đích xuất hiện trong block, surface match, không phải alignment".
  - Đổi tier policy (giữ EV-D2L-03); chỉ thêm fixes cleanup linguistic.

## 3. Spec *(Claude viết)*

**`pipeline/eval/surface_match.py` (MỚI, dùng chung):**
- `MASK_PATTERNS` (chuyển từ scorer/overlay về đây, 1 bản duy nhất).
- `mask_non_prose(text) -> str` (length-preserving, lru_cache).
- `normalize_surface(text) -> str` (NFC + apostrophe, **length-preserving**, KHÔNG casefold).
- `find_spans(text, needle, *, case_sensitive=False) -> list[tuple[int,int,str]]`: match trên `mask_non_prose(normalize_surface(text))` bằng pattern word-boundary + cờ `re.IGNORECASE` khi `not case_sensitive`; offset trùng text gốc (vì mọi normalize đều length-preserving).
- `allocate_spans(text, owners) -> dict[str, list[tuple[int,int,str]]]`: `owners` = list `(owner_id, needle, case_sensitive)`; gom mọi span, sort theo `(end-start)` desc rồi vị trí, cấp phát non-overlap, trả per-owner.

**Scorer `d2l_translate_score.py`:**
- Bỏ `_mask_non_prose`/`_find_surface_matches`/`_count_non_overlapping_forms` cục bộ → gọi `surface_match`.
- `_score_registry_consistency`: với MỖI block, dựng `owners` nguồn = mọi `(term, source_term, cs)` → `allocate_spans` → occurrence của term = span được cấp (machine learning giành span chứa learning). Tương tự đích: owners = `(term, form, cs)` cho mọi form của các term-có-mặt → `allocate_spans` → forms_used per term = span đích được cấp. ⇒ "học máy" thuộc machine learning, "học" còn lại thuộc learning; learning KHÔNG còn ăn ké.
- metric_version `d2l_translate_score_v2_1`; limitations giữ + ghi "cross-term subsumption applied; vẫn surface, chưa alignment".

**Overlay `thesis_overlay.py`:**
- Bỏ bản sao mask/_find_matches → gọi `surface_match` (cùng `allocate_spans`) để highlight, **theo cùng quy tắc cấp phát** → span overlay == span scorer đếm.
- Giữ scoped per-block/chapter (perf A02): block ≤ ~0.2s, chapter ≤ ~3s.

**Test bắt buộc — hình==số cấu trúc:** `test_overlay_matches_scorer_forms`: trên ≥1 block thật chứa learning+machine learning, **tập form overlay highlight cho mỗi term == keys forms_used scorer** cho term đó trong block đó. Đây là chốt chống tái-phân-kỳ.

**Ràng buộc (LOCK):** observe⊥compute; freeze (eval-overlay only, KHÔNG ghi DB); 0 API; offset-preserving để overlay đúng vị trí.

## 4. Acceptance criteria *(lệnh chạy được)*

```bash
cd THESIS_RUNTIME_TOOL

# (1) matcher + subsumption + shared primitive
python -m pytest pipeline/tests/test_surface_match.py pipeline/tests/test_d2l_translate_score.py -v   # PASS, gồm:
#   test_source_subsumption_machine_learning : block có "machine learning" → occurrence thuộc machine learning, KHÔNG nhân đôi sang learning
#   test_target_subsumption_hoc_may          : "học máy" thuộc machine learning; "học" lẻ thuộc learning; learning KHÔNG có "học máy"
#   test_offsets_map_to_original             : span trả về trỏ đúng ký tự gốc (overlay highlight đúng chỗ)

# (2) hình==số: overlay == scorer
python -m pytest app/backend/tests/test_thesis_overlay.py -v   # gồm test_overlay_matches_scorer_forms

# (3) re-score 0 API
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 \
  --chapters d2l_introduction d2l_preliminaries d2l_linear_networks d2l_multilayer_perceptrons \
  --config S1 --gold-variants data/eval/d2l_gold_variants.csv \
  --out data/reports/d2l_translation_metrics_v2.json
# → metric_version=d2l_translate_score_v2_1; learning.forms_used KHÔNG còn "học máy";
#   machine learning giữ "học máy"; in delta hard-headline trước/sau (kỳ vọng đổi nhẹ — learning vốn soft)

# (4) frozen DB bất biến + full suite
python -m pytest pipeline/tests app/backend/tests -q
```

## 5. Implementation notes *(CodeX, 2026-06-18)*

### 5.1 Scope implemented

- Added shared deterministic matcher `pipeline/eval/surface_match.py`:
  - length-preserving `MASK_PATTERNS`, `mask_non_prose()`, `normalize_surface()`;
  - `find_spans()` with offset-preserving regex matching and `case_sensitive`;
  - `allocate_spans()` for longest-first, non-overlap allocation across owners.
- Refactored `pipeline/eval/d2l_translate_score.py` D-scorer to use shared matcher:
  - source spans are allocated jointly across registry terms per block, so `machine learning` owns the full source span and `learning` does not also claim it;
  - target forms are allocated jointly across terms present in each source block;
  - report version is now `d2l_translate_score_v2_1`, method `block_surface_v2_1`;
  - limitations now explicitly say this remains surface matching, not alignment.
- Refactored `app/backend/services/thesis_overlay.py` to use the same matcher:
  - removed local URL/code/math mask copy and local regex matcher;
  - source and target overlay use joint allocation, matching scorer behavior;
  - app runtime can import `pipeline.eval.surface_match` when launched from `app/backend`.
- Added eval-only cleanup:
  - `data/eval/d2l_glossary_fixes.csv`: `learning,remove_variant,học máy`.
  - Frozen runtime DB was not written.
- Updated score readmodel label to `D_surface_v2.1 (hard-tier)`.

### 5.2 Re-score output

Command:

```bash
python -m pipeline.scripts.score_run --db data/jobs/d2l_p1/memory.sqlite3 --experiment d2l_p3 --chapters d2l_introduction d2l_preliminaries d2l_linear_networks d2l_multilayer_perceptrons --config S1 --gold-variants data/eval/d2l_gold_variants.csv --out data/reports/d2l_translation_metrics_v2.json
```

Output:

```text
S0: B flat=0.7541 (2322 pairs), B recurring=0.7540 (2285 pairs), D=0.7576 (396 terms)
S1: B flat=0.8191 (2322 pairs), B recurring=0.8214 (2285 pairs), D=0.9066 (396 terms)
S1 A registry TAR=0.9524 (8370 pairs)
```

Measured runtime after token-index prefilter: about 126s on this machine. This is 0 API and deterministic, but still slower than ideal; I am flagging it as a residual perf note rather than widening this task further.

### 5.3 Spot checks from report and overlay

- `metric_version`: `d2l_translate_score_v2_1`.
- `S1 learning`: `soft`, `drift`, `forms_used={"học": 18, "quá trình học": 2}`; **no `học máy`**.
- `S1 machine learning`: `hard`, `drift`, `forms_used={"học máy": 56, "máy học": 1, "machine learning": 1}`.
- `S1 calculus`: `hard`, `consistent`, `forms_used={"giải tích": 6}`.
- Real block `d2l_introduction_index_b003`:
  - source overlay: `gl_machine_learning -> ["machine learning"]`, `gl_learning -> []`;
  - target overlay S0/S1: `gl_machine_learning -> ["học máy"]`, `gl_learning -> []`.

### 5.4 Frozen DB guard

`glossary_entries` hash before and after re-score:

```text
58e7797059ec3adbf041cd528efa9a671d3af00c60cf9ac3c0485d07eb5bacdf
```

No runtime DB mutation.

### 5.5 Verification

```bash
python -m pytest -p no:cacheprovider pipeline\tests\test_surface_match.py pipeline\tests\test_d2l_translate_score.py -q
# 16 passed in 7.88s

python -m pytest -p no:cacheprovider app\backend\tests\test_thesis_overlay.py -q
# 5 passed in 1.03s

python -m pytest -p no:cacheprovider app\backend\tests\test_thesis_overlay.py app\backend\tests\test_thesis_scores.py -q
# 15 passed in 1.60s

python -m pytest -p no:cacheprovider pipeline\tests app\backend\tests -q
# 268 passed in 147.82s (0:02:27)
```

Pytest still emits the known Windows temp symlink cleanup `PermissionError` after completion; exit code is 0 and tests pass.

## 6. Review *(Claude điền)*
