# TASK_P1_01_ingest_document_loader — Nguồn Treasure Island sạch + loader document.json → SQLite blocks

- **Status:** REVIEW
- **Refs:** THESIS_ARCHITECTURE_LOCK §8.1#3 (canonical tool-generated dùng được), §6.3 (CẤM nạp annotation oracle vào thesis), §9 P1, V3 Directional Lock §0; schema blocks = `pipeline/memory/schema_v2_base.sql`
- **Branch/Commit:** branch `main`; commit pending

## 1. Bối cảnh & mục tiêu

Mở màn P1. Nguồn Treasure Island đã có cấu trúc canonical chuẩn 1.5.0 (40 chương,
1.476 blocks) do tool extraction sinh trong AILAB_HANDOFF — **dùng lại cấu trúc này**
(giữ nguyên trục `block_id` trùng với oracle run để sau so sánh được), nhưng file đang
nhúng `annotations` của oracle GPT-5.5 → **bắt buộc strip** trước khi vào thesis
(Directional Lock: World Builder phải tự xây memory từ text trắng). Sau đó load vào
bảng `documents`/`blocks` của memory.sqlite3 (schema v3, P0-01) — trục align cho toàn
bộ pipeline. KHÔNG gọi LLM.

## 2. Scope

**IN:**
1. `pipeline/scripts/prepare_source.py` — copy-and-strip tái lập được:
   `--from <path document.json>` `--to data/sources/<doc_id>/document.json`
   - Giữ nguyên: schema_version, doc_id, metadata, chapters (chapter_id, order_index,
     title), blocks (block_id, order_index, page_ids, block_type, is_chapter_opening,
     source_text, clean_text, sentences, quality_flags).
   - **Strip: field `annotations` của MỌI block → `{}`** (hoặc bỏ hẳn key — chọn 1,
     nhất quán). `sentences` GIỮ (tool-generated tier A, span code tính).
   - Ghi kèm `data/sources/<doc_id>/PROVENANCE.md`: nguồn gốc file, ngày copy,
     "annotations stripped theo LOCK §6.3", sha256 file gốc và file sau strip.
2. Chạy script đó cho Treasure Island:
   from = `../AILAB_HANDOFF/ailab_projects/treasure_island/canonical/document.json`
   → `THESIS_RUNTIME_TOOL/data/sources/treasure_island/document.json` (~2MB, ĐƯỢC track
   git — là input thí nghiệm, cần pin; `data/jobs/` mới là vùng gitignore).
3. `pipeline/ingest/document_loader.py`:
   - `load_document(db_path, document_json_path) -> LoadReport` (dataclass:
     `doc_id, chapters, blocks, warnings: list[str]`).
   - Ghi `documents`: doc_id, job_id (= doc_id nếu chưa có khái niệm job),
     source_filename, source_lang/target_lang từ metadata, metadata_json = metadata.
   - Ghi `blocks` từng block:
     - `block_id` giữ NGUYÊN VẸN từ file (trục align bất biến).
     - **`order_index` = thứ tự TOÀN CỤC** (counter chạy qua các chương theo thứ tự
       file) — KHÔNG dùng order_index per-chapter của file, vì `entity_relations`
       so pha theo order_index toàn document. Ghi rõ điều này trong docstring.
     - `text` = clean_text (trục annotation/span); `original_text` = source_text;
       `block_type`, `chapter_id` map thẳng.
     - `style_json` = JSON `{"is_chapter_opening":..., "page_ids":..., "quality_flags":...}`
       (schema v2 không có cột riêng — dùng style_json làm túi extras, docstring ghi rõ).
   - **Idempotent**: chạy lại trên cùng input → nội dung bảng giống hệt
     (INSERT OR REPLACE; so sánh bỏ qua created_at/updated_at).
   - Validation trong loader: trùng `block_id` trong file → raise ValueError;
     block_type ∈ {paragraph, dialogue} mà clean_text rỗng → thêm warning vào report
     (không raise); **nếu block còn `annotations` khác rỗng → raise** (chốt chặn
     Directional Lock ở tầng load — file chưa strip thì không được vào DB).
   - Chapter metadata KHÔNG có bảng riêng: derive từ blocks khi cần (heading block +
     chapter_id) — ghi chú docstring, không tạo bảng mới.
4. `pipeline/scripts/ingest_document.py` — CLI:
   `--source data/sources/treasure_island/document.json --db data/jobs/<job>/memory.sqlite3`
   → init_db nếu chưa có, load, in LoadReport dạng JSON.
5. Tests `pipeline/tests/test_document_loader.py` + fixture mini
   (`pipeline/tests/fixtures/mini_document.json`: 2 chương ~6 blocks, clean_text có
   tiếng Việt + ký tự đặc biệt như “quote”, em-dash, để test UTF-8 nguyên vẹn).

**OUT:** adapter D2L markdown (P1-02); re-extraction EPUB (dùng canonical sẵn);
pre-pass/spans/World Builder (P2); Chroma; KHÔNG đụng `app/`; KHÔNG sửa gì trong
AILAB_HANDOFF (chỉ ĐỌC file canonical 1 lần qua prepare_source).

## 3. Spec — chi tiết mapping

| document.json | bảng `blocks` | Ghi chú |
|---|---|---|
| block_id | block_id (PK) | nguyên vẹn, vd `treasure_island_ch02_b003` |
| (counter toàn cục) | order_index | KHÔNG lấy per-chapter index của file |
| clean_text | text | trục annotation — byte-exact, không normalize gì thêm |
| source_text | original_text | |
| block_type | block_type | |
| chapter (chapter_id) | chapter_id | |
| is_chapter_opening, page_ids, quality_flags | style_json (JSON) | túi extras |
| annotations | **không map** — phải rỗng, khác rỗng → raise | Directional Lock |
| sentences | không map vào blocks (P2 dùng trực tiếp từ file nguồn nếu cần) | |

## 4. Acceptance criteria (lệnh chạy được, offline)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

# 0. Chuẩn bị nguồn (chạy thật 1 lần, output tracked):
python -m pipeline.scripts.prepare_source --from "../AILAB_HANDOFF/ailab_projects/treasure_island/canonical/document.json" --to data/sources/treasure_island/document.json
python - <<'EOF'
import json
d = json.load(open('data/sources/treasure_island/document.json', encoding='utf-8'))
assert all(not b.get('annotations') for c in d['chapters'] for b in c['blocks'])
print('annotations stripped OK')
EOF

python -m pytest pipeline/tests/test_document_loader.py -v
# PHẢI PASS (CodeX viết):
# 1. test_load_fixture_counts_and_mapping: mini fixture → đúng số chương/block,
#    text == clean_text byte-exact (kể cả tiếng Việt/“quotes”), original_text,
#    chapter_id, block_type, style_json extras đúng
# 2. test_global_order_monotonic: order_index tăng chặt 0..N-1 xuyên các chương
# 3. test_idempotent_reload: load 2 lần → SELECT các cột ổn định (trừ timestamps)
#    giống hệt nhau
# 4. test_duplicate_block_id_raises: fixture lỗi → ValueError
# 5. test_unstripped_annotations_raises: fixture có annotations content → raise
#    (chốt chặn Directional Lock)
# 6. test_load_real_treasure_island (pytest.mark.skipif nếu thiếu file):
#    40 chương, 1476 blocks; spot-check text của treasure_island_ch02_b003 khớp nguồn

python -m pytest pipeline/tests/ -v   # toàn bộ (migration + llm_client + loader) PASS
```

## 5. Implementation notes *(CodeX điền)*

- Added `pipeline/scripts/prepare_source.py` to copy a canonical `document.json`, preserve
  the allowed source-structure fields, set every block `annotations` to `{}`, and write
  `PROVENANCE.md` with source/target paths, timestamp, and sha256 hashes.
- Ran the prepare script for Treasure Island, producing
  `data/sources/treasure_island/document.json` and
  `data/sources/treasure_island/PROVENANCE.md`. `AILAB_HANDOFF` was read-only.
- Added `pipeline/ingest/document_loader.py` with `load_document(db_path,
  document_json_path) -> LoadReport`. It writes `documents`/`blocks`, preserves `block_id`,
  uses global 0..N-1 `order_index`, stores source extras in `style_json`, raises on
  duplicate `block_id`, and raises if annotations are not stripped.
- Added `pipeline/scripts/ingest_document.py` CLI and ignored `data/jobs/` runtime DBs.
- Added `pipeline/tests/fixtures/mini_document.json` and
  `pipeline/tests/test_document_loader.py`.

Test/output:

```bash
cd C:\work\odl-pdf-demo\research\agent-based-translation\THESIS_RUNTIME_TOOL
python -m pipeline.scripts.prepare_source --from "../AILAB_HANDOFF/ailab_projects/treasure_island/canonical/document.json" --to data/sources/treasure_island/document.json
# Prepared stripped source: data\sources\treasure_island\document.json

@'
import json
d = json.load(open('data/sources/treasure_island/document.json', encoding='utf-8'))
assert all(not b.get('annotations') for c in d['chapters'] for b in c['blocks'])
print('annotations stripped OK')
'@ | python -
# annotations stripped OK

python -m pipeline.scripts.ingest_document --source data/sources/treasure_island/document.json --db data/jobs/treasure_island_p1/memory.sqlite3
# {"doc_id": "treasure_island", "chapters": 40, "blocks": 1476, "warnings": []}

python -m pytest pipeline/tests/test_document_loader.py -v
# 6 passed in 3.30s

python -m pytest pipeline/tests/ -v
# 23 passed in 7.39s
```

## 6. Review *(Claude điền — 2026-06-12)*

- **Verdict: PASS**
- Tự chạy lại: assert strip trên `data/sources/treasure_island/document.json` →
  "annotations stripped OK | 40 chapters, 1476 blocks"; 23/23 pipeline tests PASS.
- Đối chiếu spec §2–§3 (đọc `prepare_source.py` + `document_loader.py` đầy đủ):
  prepare_source copy theo whitelist key đúng danh sách, mọi block `annotations = {}`,
  PROVENANCE.md đủ nguồn gốc + timestamp + sha256 gốc/sau-strip; loader giữ `block_id`
  nguyên vẹn, `order_index` = counter TOÀN CỤC 0..N-1 (docstring ghi rõ lý do
  entity_relations), `text` = clean_text, `original_text` = source_text, extras vào
  `style_json`, raise đúng cả hai chốt (duplicate block_id, annotations chưa strip —
  chốt chặn Directional Lock ở tầng load hoạt động), warning cho paragraph/dialogue
  rỗng, chapter metadata không tạo bảng mới.
- Deviation nhỏ chấp nhận: idempotency cài bằng documents UPSERT + `DELETE FROM blocks
  WHERE doc_id` + insert lại (thay vì INSERT OR REPLACE per-row như spec gợi ý) — kết
  quả tương đương và sạch hơn (block biến mất khỏi nguồn không để lại mồ côi trong DB).
- Lưu ý cho phase sau (không chặn): (1) reload xóa-ghi-lại toàn bộ blocks của doc — khi
  P2 có bảng con FK trỏ vào blocks (spans/annotations thesis), reload document đồng
  nghĩa phải ingest lại dữ liệu con; (2) prepare_source whitelist nghĩa là field lạ
  trong nguồn bị drop im lặng — deterministic, đúng ý strip, nhưng nếu canonical schema
  nâng version thì phải cập nhật BLOCK_KEYS.
- Trục align block_id thesis ↔ oracle đã được pin: cùng 1476 blocks, so sánh tương lai
  hợp lệ. Follow-up: không có. P1-01 xong → P1-02 (D2L adapter) là việc kế tiếp của P1.
