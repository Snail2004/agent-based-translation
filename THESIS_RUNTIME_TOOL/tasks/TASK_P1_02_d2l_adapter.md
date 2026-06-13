# TASK_P1_02_d2l_adapter — Nạp nguồn D2L (EN gốc → blocks) + glossary gold EVAL-ONLY + báo cáo coverage chọn chương

- **Status:** DONE (PASS — Claude 2026-06-13; xem §6)
- **Refs:** LOCK (cc) (nguồn D2L = MT thô, glossary = gold; reference-metrics hoãn),
  (aa) (chia vai 2 dataset; D2L = track TAR có nghĩa nhất), §6.3 (gold = EVAL-ONLY, CẤM
  bơm vào prompt runtime — như AILAB), Directional Lock (memory tự học từ text, không
  annotation người làm đầu vào); mẫu ingestion = P1-01 (`pipeline/scripts/ingest_document.py`,
  `prepare_source.py`, loader → bảng `blocks`, PROVENANCE sha256); provenance nguồn =
  `data/sources/D2L_PROVENANCE.md` (pin commit c775d6b)
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

Mở track D2L (sách kỹ thuật) — nơi giá trị TAR mạnh nhất + đánh trúng mối lo GVHD ("đầu
dịch tác tử sau tác nhân"). Khác TI: D2L có **glossary người-chuẩn** (`glossary.md`) làm
**thước TAR trung lập (cách B)** → TAR đo được CHẤT LƯỢNG TỪ VỰNG thật, không chỉ "vâng
lời" như TI.

**Ràng buộc cốt tử (user tái khẳng định):** glossary.md **CHỈ để so sánh/chấm điểm**
(EVAL-ONLY), **TUYỆT ĐỐI không ném vào model**. Registry mà S1 bơm (task sau) vẫn do
World Builder **tự học từ text D2L** (Directional Lock). Hai thứ phải tách bằng kiến trúc
+ test, không phải bằng lời hứa.

P1-02 = NỀN MÓNG (gương P1-01 cho TI): nạp EN `_origin.md` → `blocks`; parse `glossary.md`
→ gold eval-only; báo cáo coverage TOÀN BỘ chương để chốt 4 chương Tầng-2 từ SỐ THẬT.
KHÔNG dịch, KHÔNG World Builder ở task này (parsing = $0, nên nạp HẾT corpus một lần).

## 2. Scope

**IN:**
1. `pipeline/ingest/d2l_markdown_loader.py` — parse EN `_origin.md` → blocks:
   - Quét `data/sources/d2l-vi/chapter_*/` lấy MỌI file `*_origin.md` (EN = nguồn chuẩn).
     **KHÔNG đọc** `index.md`/`<section>.md` (bản VI = MT thô; Directional Lock: runtime
     không nạp bản dịch sẵn).
   - Mỗi `_origin.md` = 1 section → cắt thành blocks theo đoạn (blank-line), GIỮ thứ tự.
   - Phân loại `block_type` qua heuristic markdown: `heading` (`#`/`##`), `code` (```fence```),
     `math_block` (dòng `$$`), `image` (`![..](..)`), `label` (`:label:`/`:numref:`/`:eqlabel:`),
     còn lại = `prose`. Chỉ `prose` đặt `translation_mode='translate'`; phần khác
     `passthrough`/`skip` (TAR + dịch sau CHỈ chạy trên prose).
   - `block_id` ổn định: `d2l_<chapter_slug>_<section_slug>_bNNN`; gán `chapter_id`,
     `order_index`, `original_text` (EN). Idempotent (chạy lại không nhân đôi).
   - PROVENANCE: sha256 mỗi file nguồn + commit hash (c775d6b) ghi vào `documents`/manifest.
2. `pipeline/ingest/d2l_glossary.py` — parse `glossary.md` → gold EVAL-ONLY:
   - Đọc các bảng dưới `## A`..`## Z`, cột `English | Tiếng Việt | Thảo luận tại`.
   - Trích cặp `(source_term, target_term)`; BỎ dòng header + dòng phân cách `---`; trim
     khoảng trắng; giữ nguyên dạng (kể cả ngoặc, vd "argument (in programming)").
   - Lưu vào bảng **`reference_eval_only`** (đã có sẵn trong schema — đúng chỗ cho gold
     eval-only) HOẶC bảng gold riêng tên rõ `eval_glossary_gold`; KÈM cờ phân biệt với
     `glossary_entries` (registry sẽ-được-bơm do World Builder dựng). Ghi nguồn = D2L commit.
   - **CẤM bơm:** gold này KHÔNG bao giờ được đọc bởi đường injection (context_builder).
3. `pipeline/scripts/ingest_d2l.py` — CLI nạp TOÀN BỘ + báo cáo coverage:
   - Tạo job DB `data/jobs/d2l_p1/memory.sqlite3` qua migrations (dùng lại schema hiện có).
   - Nạp HẾT `_origin.md` mọi chương → blocks; parse glossary → gold eval-only.
   - Report `data/reports/d2l_ingest_coverage.json` (tracked), mỗi chương:
     • tổng block, block prose, ước lượng token prose;
     • `glossary_terms_total` (toàn cục) + **`terms_present_in_chapter`** = số source_term
       glossary XUẤT HIỆN trong prose EN chương đó + tổng số lần xuất hiện (term density);
     • cờ chương có thuật ngữ "agent" (mối lo GVHD).
   - In bảng xếp hạng chương theo **mật độ thuật ngữ tái xuất** (để chọn Tầng-2).
4. Tests offline 100% `pipeline/tests/test_d2l_ingest.py`:
   - parse bảng glossary đúng (gồm dòng cột Thảo luận rỗng, term nhiều từ, dòng `---`);
   - parse 1 `_origin.md` mẫu → đúng số/loại block (prose vs code/math/label/image);
   - **guard tách gold↔registry:** sau ingest, `glossary_entries` (registry bơm) RỖNG,
     gold nằm ở bảng eval-only; có test khẳng định đường injection KHÔNG truy cập gold;
   - PROVENANCE sha256 + commit hash có mặt; idempotent (chạy 2 lần = cùng số block).

**OUT (task sau, KHÔNG làm ở đây):**
- World Builder trên D2L (dựng registry bơm từ text) → P2-D2L.
- Dịch S0/S1 + chấm TAR(vs gold) trên 4 chương Tầng-2 → P3-D2L (dùng lại windower/prompt/
  runner/scoring của TI; `--sample` cho judge theo LOCK bb).
- BLEU/COMET (cần bản người `aivivn/d2l-vn`) → EV-03.
- Nạp bản VI (MT thô) → KHÔNG BAO GIỜ. KHÔNG đụng artifact TI. KHÔNG đụng `app/`.

## 3. Spec — chốt chi tiết

- **EN là nguồn chuẩn duy nhất** nạp vào runtime; VI index.md bị bỏ qua hoàn toàn.
- **Gold eval-only là bất khả xâm phạm với model:** lưu tách bảng + test guard. Đây là
  hiện thực hóa câu chốt của user "dùng gold để chấm, không ném đáp án cho model". Khi
  P3-D2L chấm TAR, ruler = gold này (cách B trung lập) — KHÁC ruler tự-bơm của TI (xem
  doctrine z-ter: không trộn 3 loại thước).
- **Term coverage là tiêu chí chọn chương:** Tầng-2 = 4 chương LIÊN TIẾP có mật độ thuật
  ngữ tái xuất cao + ≥1 chương chứa "agent". Quyết định chọn chương DỰA TRÊN report, không
  đoán trước.
- **Parsing tất định, $0 API.** Nạp cả corpus một lần (không giới hạn 1 chương) vì parse
  miễn phí → report phủ đủ 23 chương cho việc chọn.
- Block prose của D2L có thể chứa markdown inline (`*italic*`, `$math$`, `:numref:`) — GIỮ
  nguyên trong `original_text`; việc xử lý lúc dịch là của P3-D2L, không phải task này.

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_d2l_ingest.py -v
# PHẢI PASS (offline): parse glossary, parse _origin.md block types, guard gold↔registry
# tách biệt (glossary_entries rỗng + gold không-injectable), provenance sha256, idempotent

python -m pipeline.scripts.ingest_d2l --src data/sources/d2l-vi --db data/jobs/d2l_p1/memory.sqlite3 --out data/reports/d2l_ingest_coverage.json
# - exit 0; tạo job DB; nạp blocks mọi chương; parse glossary N terms (~vài trăm)
# - report có: per-chapter block/prose/token + terms_present_in_chapter + density + cờ "agent"
# - in bảng xếp hạng chương theo mật độ thuật ngữ (để chốt Tầng-2)
# - glossary_entries (registry bơm) RỖNG; gold ở bảng eval-only

python -m pipeline.scripts.ingest_d2l ... (chạy lại)   # idempotent: cùng số block, không nhân đôi
python -m pytest pipeline/tests/ -v   # toàn bộ vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

- Added migration `pipeline/memory/migrations/006_eval_glossary_gold.sql` and wired it through
  `pipeline/memory/store_init.py`. D2L glossary gold is stored in `eval_glossary_gold`, not
  `glossary_entries`; the latter remains the runtime registry for World Builder output only.
- Added `pipeline/ingest/d2l_markdown_loader.py`:
  - reads only `*_origin.md` under `data/sources/d2l-vi/chapter_*/`;
  - ignores Vietnamese `.md` files;
  - preserves book/chapter order from D2L `index.md`/chapter TOCs;
  - splits markdown deterministically by blank lines while preserving fenced code/math blocks;
  - writes EN source blocks to `documents`/`blocks` with stable ids
    `d2l_<chapter_slug>_<section_slug>_bNNN`;
  - marks only `prose` as `translation_mode='translate'`; heading/code/math/image/label are
    `passthrough`.
- Added `pipeline/ingest/d2l_glossary.py`:
  - parses `glossary.md` tables under `## A`..`## Z`;
  - skips headers/separators and dedupes repeated `(source_term, target_term)` pairs observed in
    the real glossary;
  - stores 458 eval-only gold terms with source commit/path/line provenance.
- Added `pipeline/scripts/ingest_d2l.py`:
  - initializes/migrates the job DB;
  - loads the full D2L source snapshot;
  - stores glossary gold in `eval_glossary_gold`;
  - emits `data/reports/d2l_ingest_coverage.json` with per-chapter block/prose/token counts,
    glossary term coverage, density, and `has_agent_term`.
- Added `pipeline/tests/test_d2l_ingest.py` with parser tests, block-type tests, idempotency,
  provenance, coverage, and the required guard proving `context_builder` does not read gold:
  `glossary_entries` stays empty while `eval_glossary_gold` is populated.

Real ingest output:

```bash
cd C:\Users\nguye\OneDrive\Tài liệu\Baitap\DuAnCNTT\odl-pdf-demo\research\agent-based-translation\THESIS_RUNTIME_TOOL
python -m pipeline.scripts.ingest_d2l --src data/sources/d2l-vi --db data/jobs/d2l_p1/memory.sqlite3 --out data/reports/d2l_ingest_coverage.json
```

```text
{
  "doc_id": "d2l",
  "chapters": 23,
  "loaded_chapters": 22,
  "sections": 165,
  "blocks": 8803,
  "prose_blocks": 4609,
  "glossary_gold_entries": 458,
  "report": "data\\reports\\d2l_ingest_coverage.json"
}

Top chapters by glossary term density:
d2l_attention_mechanisms: density=39.50 occ=529 terms=62 agent=False
d2l_linear_networks: density=38.38 occ=837 terms=73 agent=False
d2l_generative_adversarial_networks: density=36.81 occ=127 terms=42 agent=False
d2l_convolutional_modern: density=36.76 occ=622 terms=76 agent=False
d2l_computer_vision: density=34.99 occ=1083 terms=84 agent=False
d2l_multilayer_perceptrons: density=34.97 occ=1169 terms=108 agent=False
d2l_convolutional_neural_networks: density=33.22 occ=498 terms=57 agent=False
d2l_notation: density=29.03 occ=29 terms=13 agent=False
d2l_deep_learning_computation: density=28.06 occ=345 terms=40 agent=False
d2l_natural_language_processing_applications: density=27.55 occ=294 terms=49 agent=False
```

DB guard spot-check:

```text
blocks 8803
glossary_entries 0
eval_glossary_gold 458
prose 4609
translate_mode [('passthrough', 4194), ('translate', 4609)]
warnings ['no_origin_sections:chapter_references']
agent chapters [('d2l_introduction', 528, 26.6451), ('d2l_preliminaries', 504, 22.9174)]
```

Notes/deviations:
- Source snapshot has 23 `chapter_*` directories, but `chapter_references` contains only
  `zreferences.md` and no `_origin.md`; loader reports `chapters=23`, `loaded_chapters=22`,
  and warning `no_origin_sections:chapter_references`. No synthetic reference blocks were created.
- Existing `reference_eval_only` is block-target oriented and has no `source_term` column, so a
  dedicated eval-only table `eval_glossary_gold` is cleaner than stuffing glossary pairs into
  `target_text`.

Tests:

```text
python -m pytest pipeline/tests/test_d2l_ingest.py -v
# 11 passed in 3.20s

python -m pipeline.scripts.ingest_d2l ...  # run twice on same DB
# identical counts both times: 23 chapters / 22 loaded / 165 sections / 8803 blocks / 458 gold

python -m pytest pipeline/tests/ -v
# 97 passed in 59.79s
```

Windows note: pytest still prints the known `PermissionError` cleanup warning for
`D:\temp\pytest-of-Snail\pytest-current` after exit, but pytest exits 0 and all tests pass.

## 6. Review *(Claude điền)*

**Verdict: PASS** (Claude, 2026-06-13). Nền móng D2L vững; ràng buộc cốt tử (gold eval-only,
không bơm vào model) được hiện thực hóa bằng **kiến trúc + test hành vi**, không phải lời hứa.
Số tái tính độc lập từ DB, KHỚP 100%.

### 6.1 Tái xác minh độc lập (từ `data/jobs/d2l_p1/memory.sqlite3`)
- blocks 8803 / prose(translate) 4609 / passthrough 4194 / 22 chương — KHỚP §5.
- `eval_glossary_gold` 458 / `glossary_entries` (registry bơm) **0** — KHỚP.
- block_type: prose 4609, code 2371, heading 1293, math 286, image 215, label 29 (tổng 8803).
- "agent"→"tác nhân" có trong gold; chương cờ agent: introduction (528 occ), preliminaries (504).

### 6.2 Audit guard tách gold↔registry (ràng buộc quan trọng nhất của user) — ĐẠT loại mạnh
- `grep eval_glossary_gold` toàn pipeline: chỉ d2l_glossary.py (ghi) + store_init.py (migration)
  + tests. **context_builder/prompt/runner KHÔNG hề tham chiếu** gold (grep rỗng).
- `test_gold_eval_only_not_injected_into_context` không chỉ đếm `glossary_entries==0` mà CHẠY
  thật đường injection (`plan_anchors`/`build_context_pack`) trên dữ liệu D2L → khẳng định
  `term_counts=={}` + `glossary_lines==[]`. Đây là bằng chứng HÀNH VI rằng không gì bị ném
  vào model — đúng y câu chốt "dùng gold để chấm, không ném đáp án cho model".

### 6.3 Đánh giá lệch spec — TÁN THÀNH
- CodeX KHÔNG dùng `reference_eval_only` (nó block-target, không có cột source_term) mà tạo
  bảng riêng `eval_glossary_gold`. Sạch hơn đề xuất trong spec → ĐỒNG Ý.
- `chapter_references` không có `_origin.md` → loaded 22/23 + warning, KHÔNG tạo block giả.
  Xử lý trung thực (references là thư mục, không phải prose để dịch).

### 6.4 Ghi chú nhỏ (không chặn PASS)
- migration 006 đặt `schema_version='3'` (nhãn thế hệ schema, không phải số migration) — mỹ
  thuật, không ảnh hưởng (006 đã chạy, bảng tồn tại 458 dòng).
- Đếm term-coverage là heuristic để CHỌN chương (không phải metric chấm điểm) → sai số nhỏ
  chấp nhận; khi P3-D2L chấm TAR thật phải dùng matching chuẩn (word-boundary + apostrophe).

### 6.5 Dữ liệu chọn Tầng-2 (chốt ở spec P3-D2L)
Mật độ thuật ngữ cao: multilayer_perceptrons (1169 occ), computer_vision (1083), linear_networks
(837), attention (529, density cao nhất). "agent" chỉ ở introduction + preliminaries.
**Đề xuất run liên tiếp:** introduction → preliminaries → linear-networks → multilayer-perceptrons
(2 chương agent + 2 chương đặc thuật-ngữ nhất; ~1019 prose). Nếu pilot đầu cần gọn: introduction
+ linear-networks (~380 prose). Lock cuối ở P3-D2L kèm `--sample` judge (LOCK bb).

### 6.6 Follow-up
- **P2-D2L:** World Builder dựng registry bơm từ text D2L (Directional Lock).
- **P3-D2L:** dịch S0/S1 chương Tầng-2 + chấm **TAR vs gold** (cách B trung lập) + judge sample.

