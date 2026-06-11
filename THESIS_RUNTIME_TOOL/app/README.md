# App Notes

`WEB_TOOL_SPEC.md` là source chính cho app AI-LAB Dataset Tool.

App này là local-first dataset construction tool:

```text
Upload source -> Extract -> Review/Clean -> Annotate -> Validate -> Export/Freeze
```

Không phải app dịch, không phải app OCR, không phải PDF layout editor.

## Kiến Trúc Khuyến Nghị

- Backend: Python Flask.
- Frontend: vanilla HTML/CSS/JS.
- Data: local filesystem project workspace.
- Validation: backend gọi `dataset_spec/tools/validate.py`.

## Module Code Gợi Ý

| Module | Trách nhiệm |
|---|---|
| Project manager | tạo/mở project, upload source, provenance |
| Extraction backend | TXT/EPUB -> `document.json` |
| Block UI | chapter/block browser, clean editor |
| Annotation UI | glossary/entity/summary/reference |
| Validation/export | validate, error view, freeze/export |
| Persistence | autosave, logs, resume |

## UI Prototype

`prototype/` chứa bản UI prototype tải từ Claude Design. Đây là wireframe tương tác để tham khảo khi code frontend thật, chưa phải backend hoàn chỉnh.

Chạy xem nhanh:

```powershell
cd app/prototype
python -m http.server 8765 --bind 127.0.0.1
```

Mở `http://127.0.0.1:8765/index.html`.

## Backend Phase 1

`backend/` chứa Flask API skeleton local cho AI-LAB Dataset Tool. Backend đọc dataset thật từ filesystem workspace và gọi `dataset_spec/tools/validate.py --json`.

Chạy:

```powershell
cd app\backend
python -m pip install -r requirements.txt
python app.py
```

API mặc định:

```text
http://127.0.0.1:5000/api
```

Smoke checks:

```powershell
Invoke-RestMethod http://127.0.0.1:5000/api/health
Invoke-RestMethod http://127.0.0.1:5000/api/projects
Invoke-RestMethod http://127.0.0.1:5000/api/projects/gold_demo_01
Invoke-RestMethod http://127.0.0.1:5000/api/projects/gold_demo_01/dataset
Invoke-RestMethod -Method Post http://127.0.0.1:5000/api/projects/gold_demo_01/validate
```

Lần chạy đầu, backend seed `gold_demo_01` từ `dataset_spec/sample/gold_demo_01` vào `ailab_projects/gold_demo_01`. Workspace này là dữ liệu runtime local và đã được Git ignore.

## Backend Phase 2

Phase 2 thêm các endpoint ghi chỉnh sửa thật xuống filesystem bằng atomic write. Các endpoint này vẫn giữ nguyên schema dataset hiện tại.

Endpoints chính:

```text
PATCH  /api/projects/<doc_id>/metadata
PATCH  /api/projects/<doc_id>/blocks/<block_id>
PATCH  /api/projects/<doc_id>/review/blocks/<block_id>
POST   /api/projects/<doc_id>/glossary/from-selection
PATCH  /api/projects/<doc_id>/glossary/<term_id>
DELETE /api/projects/<doc_id>/glossary/<term_id>
POST   /api/projects/<doc_id>/entities/from-selection
PATCH  /api/projects/<doc_id>/entities/<entity_id>
PATCH  /api/projects/<doc_id>/summary/<chapter_id>
```

Nguyên tắc:

- Không cho sửa các field read-only như `doc_id`, `schema_version`, `block_id`, `source_text`, `provenance`.
- `from-selection` nhận `start/end` từ UI text selection và backend ghi span vào JSONL.
- Sửa `clean_text` sẽ trả về `stale_spans` nếu span hiện có không còn khớp, đồng thời đánh dấu `needs_retag` trong `working/review_state.json`.
- Delete glossary bị chặn nếu term đang `locked`, còn occurrence, hoặc còn được block tham chiếu.
- Mỗi mutation đều ghi `logs/app_events.jsonl`.

Chạy test backend:

```powershell
python -m unittest discover app\backend\tests
```

## Backend Phase 3

Phase 3 thêm project/source/extraction/job endpoints. Extraction chạy bằng backend Python, không parse trong browser.

Endpoints:

```text
POST /api/projects
POST /api/projects/<doc_id>/source
POST /api/projects/<doc_id>/extract
GET  /api/projects/<doc_id>/jobs/<job_id>
```

Scope MVP:

- Nhận `.txt` và `.epub`.
- Từ chối `.pdf` trong nhánh này vì app không làm OCR/PDF layout editor.
- Re-extract yêu cầu `overwrite=true` để tránh ghi đè nhầm `document.json`.
- Mỗi extraction ghi job record trong `working/jobs/` để tắt app mở lại vẫn xem được trạng thái.

## Backend Phase 4

Phase 4 thêm reference lifecycle, export và freeze gate.

Endpoints:

```text
POST /api/projects/<doc_id>/references/draft
POST /api/projects/<doc_id>/references/<reference_id>/review
POST /api/projects/<doc_id>/references/<reference_id>/lock
POST /api/projects/<doc_id>/export
POST /api/projects/<doc_id>/freeze
```

Reference draft nằm trong `working/drafts.json` và `working/translation_review_log.csv`. Chỉ reference `reviewed` hoặc `locked` mới được ghi vào `canonical/manual_reference_subset.jsonl`.

Freeze bị chặn nếu còn:

- validator error;
- block chưa review;
- block cần re-tag span;
- draft reference;
- thiếu reference reviewed/locked;
- thiếu chapter summary;
- metadata/provenance bắt buộc chưa đủ.

Export khác Freeze:

| Action | Điều kiện | Output |
|---|---|---|
| Export | Có thể còn lỗi | zip backup/debug hiện trạng |
| Freeze | Gate pass | zip versioned + manifest chính thức |

## Backend Phase 5 / Handoff

Trước khi giao nhóm dùng:

1. Cài requirements.
2. Chạy backend.
3. Mở frontend prototype hoặc client thật.
4. Tạo project.
5. Upload/extract source.
6. Annotate/edit.
7. Validate.
8. Export hoặc Freeze.
9. Backup cả thư mục `ailab_projects/` nếu cần giữ tiến độ local.

Kiểm tra nhanh toàn backend:

```powershell
python -m unittest discover app\backend\tests
python dataset_spec\tools\validate.py --dataset dataset_spec\sample\gold_demo_01 --schema dataset_spec\schema --json
```
