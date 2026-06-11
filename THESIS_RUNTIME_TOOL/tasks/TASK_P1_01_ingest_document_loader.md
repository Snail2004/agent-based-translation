# TASK_P1_01_ingest_document_loader — Nguồn Treasure Island sạch + loader document.json → SQLite blocks

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §8.1#3 (canonical tool-generated dùng được), §6.3 (CẤM nạp annotation oracle vào thesis), §9 P1, V3 Directional Lock §0; schema blocks = `pipeline/memory/schema_v2_base.sql`
- **Branch/Commit:** (điền khi imple xong; commit `P1-01: ...`)

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

—

## 6. Review *(Claude điền)*

—
