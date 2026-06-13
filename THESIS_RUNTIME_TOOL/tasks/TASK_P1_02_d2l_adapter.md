# TASK_P1_02_d2l_adapter — Nạp nguồn D2L (EN gốc → blocks) + glossary gold EVAL-ONLY + báo cáo coverage chọn chương

- **Status:** READY
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

—

## 6. Review *(Claude điền)*

—
