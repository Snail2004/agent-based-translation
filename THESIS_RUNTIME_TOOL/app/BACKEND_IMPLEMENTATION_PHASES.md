# AI-LAB Dataset Tool - Backend Implementation Phases

File này là kế hoạch triển khai backend cho `AILAB Dataset Tool`. Mục tiêu là giúp nhóm implement đồng bộ theo từng giai đoạn, không làm lệch scope AI-LAB và không làm hỏng dataset format hiện tại.

## 0. Nguyên tắc chung

### Scope

Backend phục vụ một app local-first để tạo, sửa, kiểm tra và freeze dataset EN->VI văn bản dài theo schema hiện tại.

Backend **không** làm các việc sau trong MVP:

- Không làm app dịch tự động.
- Không làm OCR.
- Không làm PDF layout editor.
- Không thêm database.
- Không đổi schema version.
- Không đổi format dataset.
- Không đưa draft chưa review vào JSONL freeze-clean.

### Kiến trúc chuẩn

```text
Frontend prototype
  -> HTTP API
  -> Flask backend
  -> project workspace trên filesystem
  -> validate.py --json
```

MVP dùng filesystem thay vì database. Mỗi dataset source là một thư mục project độc lập.

```text
projects/
  <doc_id>/
    raw/
      source.epub
      source.txt
    canonical/
      document.json
      glossary.jsonl
      entities.jsonl
      chapter_summaries.jsonl
      manual_reference_subset.jsonl
    working/
      review_state.json
      translation_review_log.csv
      drafts.json
      jobs/
        extract_<job_id>.json
    logs/
      app_events.jsonl
    exports/
      <doc_id>_v001.zip
```

### Ranh giới canonical vs working

| Khu vực | Ý nghĩa | Luật |
|---|---|---|
| `raw/` | File nguồn gốc | Không sửa tay, chỉ thay khi upload/re-extract có confirm |
| `canonical/` | Dataset files theo schema | Phải validate được; chỉ chứa dữ liệu đủ sạch |
| `working/` | Draft, review state, tiến độ UI | Có thể chưa validate; dùng để resume |
| `logs/` | Lịch sử thao tác | Append-only JSONL |
| `exports/` | Bản freeze/version | Chỉ tạo khi gate pass |

### Atomic write bắt buộc

Mọi ghi file JSON/JSONL/CSV phải dùng atomic write:

```text
write <file>.tmp
flush/close
replace <file>.tmp -> <file>
```

Không ghi trực tiếp đè lên file chính. Nếu app tắt giữa chừng, file chính vẫn không bị hỏng.

### Response envelope khuyến nghị

API nên trả JSON thống nhất:

```json
{
  "ok": true,
  "data": {},
  "errors": [],
  "warnings": []
}
```

Lỗi:

```json
{
  "ok": false,
  "data": null,
  "errors": [
    {
      "code": "missing_project",
      "message": "Project not found",
      "file": null,
      "location": null
    }
  ],
  "warnings": []
}
```

## 1. Phase 1 - Backend skeleton + đọc sample thật

### Mục tiêu

Mở được một project dataset hiện có, đọc các file thật, trả về đúng dữ liệu cho UI, và chạy validator thật bằng `validate.py --json`.

Phase này chưa cần ghi chỉnh sửa thật.

### Deliverables

```text
app/backend/
  app.py
  config.py
  services/
    workspace.py
    dataset_io.py
    validator.py
    audit_log.py
  routes/
    projects.py
    dataset.py
    validation.py
  tests/
```

### Filesystem bootstrap

Backend nên hỗ trợ ít nhất một project seed từ golden sample:

```text
projects/gold_demo_01/canonical/
  document.json
  glossary.jsonl
  entities.jsonl
  chapter_summaries.jsonl
  manual_reference_subset.jsonl
```

Nếu chưa có `projects/`, backend có thể tạo từ `dataset_spec/sample/gold_demo_01`.

### API cần có

#### `GET /api/health`

Trả:

```json
{ "ok": true, "data": { "status": "ready" }, "errors": [], "warnings": [] }
```

#### `GET /api/projects`

Trả danh sách project:

```json
{
  "ok": true,
  "data": [
    {
      "doc_id": "gold_demo_01",
      "title": "[SYNTHETIC DEMO] The Turning",
      "status": "available",
      "path": "projects/gold_demo_01"
    }
  ],
  "errors": [],
  "warnings": []
}
```

#### `GET /api/projects/<doc_id>`

Trả metadata project + trạng thái file:

```json
{
  "ok": true,
  "data": {
    "doc_id": "gold_demo_01",
    "has_raw": false,
    "has_canonical": true,
    "has_working": true,
    "files": {
      "document": true,
      "glossary": true,
      "entities": true,
      "chapter_summaries": true,
      "manual_reference_subset": true
    }
  },
  "errors": [],
  "warnings": []
}
```

#### `GET /api/projects/<doc_id>/dataset`

Trả toàn bộ dữ liệu cần render UI:

```json
{
  "ok": true,
  "data": {
    "document": {},
    "blocks": [],
    "chapters": [],
    "glossary": [],
    "entities": [],
    "summaries": [],
    "references": [],
    "review_state": {}
  },
  "errors": [],
  "warnings": []
}
```

#### `POST /api/projects/<doc_id>/validate`

Gọi:

```powershell
python dataset_spec\tools\validate.py --dataset projects\<doc_id>\canonical --schema dataset_spec\schema --json
```

Trả nguyên report JSON của validator, có thể bọc trong envelope hoặc trả trực tiếp. Khuyến nghị bọc:

```json
{
  "ok": true,
  "data": {
    "ok": true,
    "counts": {},
    "errors": [],
    "warnings": []
  },
  "errors": [],
  "warnings": []
}
```

### Backend services

#### `workspace.py`

Trách nhiệm:

- Resolve project path an toàn.
- Chặn path traversal.
- Tạo folder chuẩn.
- List project.
- Copy seed sample nếu cần.

Hàm gợi ý:

```python
def list_projects() -> list[dict]: ...
def get_project_path(doc_id: str) -> Path: ...
def ensure_project_dirs(doc_id: str) -> dict[str, Path]: ...
def seed_project_from_sample(doc_id: str, sample_path: Path) -> None: ...
```

#### `dataset_io.py`

Trách nhiệm:

- Read/write JSON.
- Read/write JSONL.
- Flatten document thành `chapters` + `blocks` cho UI.
- Không validate nội dung chuyên sâu ở đây.

Hàm gợi ý:

```python
def read_dataset(project_path: Path) -> dict: ...
def read_jsonl(path: Path) -> list[dict]: ...
def write_json_atomic(path: Path, data: dict) -> None: ...
def write_jsonl_atomic(path: Path, rows: list[dict]) -> None: ...
```

#### `validator.py`

Trách nhiệm:

- Gọi `validate.py --json`.
- Parse stdout JSON.
- Map exit code: `0` pass, `1` validation fail, `2` setup/internal.

Hàm gợi ý:

```python
def run_validator(dataset_dir: Path, schema_dir: Path) -> dict: ...
```

#### `audit_log.py`

Phase 1 chỉ cần helper append:

```python
def log_event(project_path: Path, event_type: str, payload: dict, user: str) -> None: ...
```

Log line:

```json
{"ts":"2026-06-01T15:30:00+07:00","user":"U2 · Mai","event":"validate","payload":{"ok":true}}
```

### Acceptance criteria

- `GET /api/health` hoạt động.
- `GET /api/projects` thấy ít nhất `gold_demo_01`.
- `GET /api/projects/gold_demo_01/dataset` trả đủ document/glossary/entities/summaries/references/review_state.
- `POST /api/projects/gold_demo_01/validate` chạy `validate.py --json` thật và trả `ok:true`.
- UI có thể thay mock `DATA.*` bằng API response sau phase này.

### Tests

```powershell
cd app\backend
python app.py
```

```powershell
curl http://127.0.0.1:5000/api/health
curl http://127.0.0.1:5000/api/projects
curl http://127.0.0.1:5000/api/projects/gold_demo_01/dataset
curl -X POST http://127.0.0.1:5000/api/projects/gold_demo_01/validate
```

## 2. Phase 2 - Ghi chỉnh sửa thật + autosave working state

### Mục tiêu

UI sửa gì thì backend ghi xuống file thật. Phase này biến prototype thành annotation tool dùng được với dataset có sẵn.

Chưa cần upload/extract nguồn mới.

### API cần có

#### `PATCH /api/projects/<doc_id>/metadata`

Patch vào `canonical/document.json.metadata`.

Editable fields:

- `title`
- `author`
- `domain`
- `genre`
- `source_format`
- `license`
- `source_url`
- `contamination_risk`

Không cho sửa:

- `doc_id`
- `schema_version`
- `raw_sha256`
- `extraction_tool`
- `pipeline_version`

#### `PATCH /api/projects/<doc_id>/blocks/<block_id>`

Editable fields:

- `clean_text`
- `block_type`
- `is_chapter_opening`
- `quality_flags`
- `discourse`

Read-only fields:

- `block_id`
- `chapter_id`
- `order_index`
- `source_text`
- `provenance`

Nếu sửa `clean_text`, backend nên trả thông tin span bị stale:

```json
{
  "ok": true,
  "data": {
    "block": {},
    "stale_spans": [
      {
        "kind": "entity",
        "id": "e_001",
        "expected_surface": "Mira",
        "span": [0, 4]
      }
    ]
  },
  "errors": [],
  "warnings": []
}
```

#### `PATCH /api/projects/<doc_id>/review/blocks/<block_id>`

Ghi vào `working/review_state.json`.

```json
{
  "reviewed": true,
  "reviewed_by": "U2 · Mai"
}
```

#### `POST /api/projects/<doc_id>/glossary/from-selection`

Input:

```json
{
  "block_id": "gold_demo_01_ch01_b002",
  "start": 17,
  "end": 28,
  "source_term": "the Turning"
}
```

Backend:

- Tạo `term_id`.
- Thêm occurrence vào `glossary.jsonl`.
- Không để LLM hoặc người nhập offset tay. Offset đến từ text selection UI.

#### `PATCH /api/projects/<doc_id>/glossary/<term_id>`

Editable:

- `source_term`
- `expected_target`
- `allowed_variants`
- `forbidden_variants`
- `status`

Guard:

- Không cho delete/update gây dangling reference.
- `locked` nên cần reviewer/lead action ở UI, nhưng backend MVP có thể chỉ log.

#### `DELETE /api/projects/<doc_id>/glossary/<term_id>`

Chặn nếu:

- `status == locked`
- còn occurrence được block trỏ tới
- reference/history đang dùng term đó

#### `POST /api/projects/<doc_id>/entities/from-selection`

Input:

```json
{
  "block_id": "gold_demo_01_ch01_b002",
  "start": 0,
  "end": 4,
  "surface": "Mira"
}
```

Backend:

- Có thể tạo entity mới hoặc thêm mention vào entity có sẵn.
- Offset từ UI selection.

#### `PATCH /api/projects/<doc_id>/entities/<entity_id>`

Editable:

- `canonical_source`
- `canonical_target`
- `aliases_target`
- `pronoun_policy`
- `gender`
- `entity_type`

#### `PATCH /api/projects/<doc_id>/summary/<chapter_id>`

Editable:

- `summary_source`
- `source`
- `characters_present`
- `setting`
- `emotional_tone`
- optional fields nếu UI mở rộng.

### Atomic write rules

Mỗi endpoint ghi file phải:

1. Đọc file hiện tại.
2. Patch trong memory.
3. Ghi file `.tmp`.
4. Replace file chính.
5. Ghi `logs/app_events.jsonl`.

Nếu bước 3/4 fail, trả lỗi và không làm hỏng file chính.

### Acceptance criteria

- Sửa metadata reload lại vẫn còn.
- Sửa `clean_text` reload lại vẫn còn.
- Mark reviewed reload lại vẫn còn trong `review_state.json`.
- Add glossary/entity từ selection ghi đúng span.
- Update summary ghi vào `chapter_summaries.jsonl`.
- Validate sau khi sửa vẫn gọi được.
- Delete term đang locked hoặc còn referenced bị chặn.

### Tests

Manual:

1. Mở project.
2. Sửa `clean_text` block bất kỳ.
3. Reload browser.
4. Kiểm tra text vẫn còn.
5. Add glossary từ selection.
6. Chạy validate.
7. Delete locked term và thấy bị chặn.

Automated nên có:

```powershell
python -m pytest app\backend\tests
```

Test cases:

- `test_patch_metadata_atomic`
- `test_patch_block_clean_text`
- `test_review_state_persists`
- `test_add_glossary_from_selection`
- `test_delete_locked_term_blocked`

## 3. Phase 3 - Upload source + extraction TXT/EPUB

### Mục tiêu

Tạo project mới từ file TXT/EPUB, sinh `document.json` draft và provenance tối thiểu.

Phase này mới nối nút `Extract` thật trong Project/Source screen.

### API cần có

#### `POST /api/projects`

Tạo project shell.

Input:

```json
{
  "doc_id": "austen_pp_en",
  "metadata": {
    "title": "Pride and Prejudice",
    "author": "Jane Austen",
    "domain": "literary_fiction",
    "genre": "novel",
    "source_format": "epub",
    "license": "public-domain",
    "source_url": "https://www.gutenberg.org/ebooks/1342",
    "contamination_risk": "low"
  }
}
```

Backend tạo:

```text
projects/<doc_id>/
  raw/
  canonical/
  working/
  logs/
  exports/
```

#### `POST /api/projects/<doc_id>/source`

Upload file vào `raw/`.

Guard:

- Chỉ nhận `.txt`, `.epub` trong MVP.
- `.pdf` nếu có thì trả lỗi rõ: `PDF logical extraction is not supported in MVP`.
- Không ghi đè source cũ nếu chưa confirm.

#### `POST /api/projects/<doc_id>/extract`

Input:

```json
{
  "overwrite": false
}
```

Nếu project đã có `canonical/document.json` và `overwrite=false`, trả lỗi:

```json
{
  "ok": false,
  "errors": [
    {
      "code": "confirm_overwrite_required",
      "message": "Re-extracting can overwrite current document draft"
    }
  ]
}
```

Nếu `overwrite=true`, tạo job:

```json
{
  "ok": true,
  "data": {
    "job_id": "extract_20260601_153000",
    "status": "queued"
  }
}
```

#### `GET /api/projects/<doc_id>/jobs/<job_id>`

Trả:

```json
{
  "ok": true,
  "data": {
    "job_id": "extract_20260601_153000",
    "type": "extract",
    "status": "done",
    "started_at": "...",
    "finished_at": "...",
    "message": "Extracted 14 blocks in 2 chapters"
  }
}
```

### Extraction MVP

#### TXT extraction

Quy tắc đơn giản:

- Split chapter bằng heading pattern:
  - `Chapter 1`
  - `CHAPTER I`
  - `Chapter One`
- Split block bằng blank line.
- Detect dialogue nếu block bắt đầu bằng quote hoặc có cấu trúc quote dominant.
- `heading` cho chapter title.
- `paragraph` mặc định.
- `footnote` nếu dòng bắt đầu bằng `[Editor's note:` hoặc tương tự.

#### EPUB extraction

Nếu dùng thư viện Python:

- `ebooklib` để đọc EPUB.
- `BeautifulSoup` để parse HTML.
- Lấy text từ heading/p.
- Preserve chapter order.

Nếu chưa kịp EPUB chuẩn, ưu tiên TXT MVP trước nhưng UI vẫn cho thấy EPUB là planned.

### Sinh `document.json`

Yêu cầu:

- `schema_version = "1.5.0"`.
- `doc_id` từ project.
- metadata từ Project/Source form.
- provenance:
  - `raw_sha256`
  - `extraction_tool`
  - `pipeline_version`
  - `retrieved_at`
- chapters >= 1.
- blocks >= 1.
- mỗi block có:
  - `block_id`
  - `chapter_id`
  - `order_index`
  - `block_type`
  - `source_text`
  - `clean_text`
  - `is_chapter_opening`
  - `quality_flags`

### Sau extraction

Backend tạo file rỗng hợp lệ nếu cần:

```text
canonical/glossary.jsonl
canonical/entities.jsonl
canonical/chapter_summaries.jsonl
canonical/manual_reference_subset.jsonl
working/review_state.json
working/translation_review_log.csv
```

Lưu ý: JSONL optional có thể vắng, nhưng tạo file rỗng giúp UI ổn định hơn.

### Acceptance criteria

- Tạo project mới từ TXT.
- Upload source vào `raw/`.
- Extract sinh `canonical/document.json`.
- Validate pass nếu chưa có sidecar hoặc sidecar rỗng.
- Re-extract không ghi đè nếu không confirm.
- Job status vẫn đọc lại được sau khi tắt app.

### Tests

- TXT có 2 chapter -> sinh 2 chapters.
- TXT có dialogue -> block_type `dialogue`.
- TXT không có chapter heading -> tạo chapter mặc định.
- Re-extract không confirm -> fail.
- Re-extract confirm -> overwrite và log event.

## 4. Phase 4 - Reference lifecycle + export/freeze

### Mục tiêu

Hoàn thiện quy trình nhóm: draft reference ở working, reviewed/locked mới vào JSONL, validate pass thì export/freeze.

### Reference lifecycle

```text
draft
  -> reviewed
  -> locked
```

Draft:

- Lưu trong `working/translation_review_log.csv` hoặc `working/drafts.json`.
- Có thể thiếu field.
- Có thể dùng AI-assisted draft.
- Không vào `manual_reference_subset.jsonl`.

Reviewed:

- Có `source`.
- Có `reference_vi`.
- Có `translated_by`.
- Có `reviewed_by`.
- Nếu có `ai_model`, bắt buộc `source = ai_assisted_verified`.
- Có thể promote vào `manual_reference_subset.jsonl`.

Locked:

- Không sửa từ UI thường.
- Chỉ lead/reviewer action mới unlock, nếu có.
- Freeze-eligible.

### API cần có

#### `POST /api/projects/<doc_id>/references/draft`

Tạo hoặc update draft trong working.

Input:

```json
{
  "block_id": "gold_demo_01_ch01_b004",
  "reference_vi": "...",
  "source": "",
  "ai_model": "claude-3.5",
  "prompt_id": "ref-draft-v1"
}
```

#### `POST /api/projects/<doc_id>/references/<reference_id>/review`

Guard:

- `reference_vi` non-empty.
- `source in {"human", "ai_assisted_verified"}`.
- Nếu `ai_model` non-empty thì `source == "ai_assisted_verified"`.
- Reviewer khác translator nếu team muốn enforce 4-eyes.

Action:

- Set `status = reviewed`.
- Set `reviewed_by`.
- Write/update `canonical/manual_reference_subset.jsonl`.
- Keep working log.

#### `POST /api/projects/<doc_id>/references/<reference_id>/lock`

Guard:

- Current status must be `reviewed`.

Action:

- Set `status = locked` in `manual_reference_subset.jsonl`.

### Freeze gate thật

`POST /api/projects/<doc_id>/freeze`

Backend phải check:

1. `validate.py --json` trả `ok:true`.
2. Không còn block unreviewed trong `working/review_state.json`.
3. Không còn draft reference bắt buộc.
4. Mọi reference trong canonical có `status in {"reviewed", "locked"}`.
5. Không còn stale spans.
6. Required metadata/provenance đủ.
7. License/source_url/contamination_risk đã được lead xác nhận nếu UI có field này.

Nếu fail:

```json
{
  "ok": false,
  "errors": [
    {
      "code": "freeze_blocked",
      "message": "Freeze blocked",
      "reasons": [
        "2 validation errors",
        "8 unreviewed blocks",
        "1 draft reference"
      ]
    }
  ]
}
```

Nếu pass:

```text
exports/<doc_id>_v001.zip
exports/<doc_id>_v001_manifest.json
```

Manifest:

```json
{
  "doc_id": "gold_demo_01",
  "version": "v001",
  "created_at": "2026-06-01T15:30:00+07:00",
  "validator_ok": true,
  "counts": {
    "chapters": 2,
    "blocks": 14,
    "terms": 2,
    "entities": 3,
    "reference": 3,
    "summaries": 2
  },
  "files": [
    "document.json",
    "glossary.jsonl",
    "entities.jsonl",
    "chapter_summaries.jsonl",
    "manual_reference_subset.jsonl"
  ]
}
```

### Export vs Freeze

| Action | Điều kiện | Output | Ý nghĩa |
|---|---|---|---|
| Export | Có thể còn lỗi | zip current package | Gửi tạm, debug, backup |
| Freeze | Gate pass | versioned zip + manifest | Bản dataset chính thức |

Export không được tự gọi là canonical/freeze nếu gate chưa pass.

### Acceptance criteria

- Draft reference reload lại vẫn còn trong working.
- Draft không xuất hiện trong `manual_reference_subset.jsonl`.
- Mark reviewed ghi vào JSONL.
- Lock chuyển status thành `locked`.
- Freeze fail nếu còn validation error.
- Freeze fail nếu còn unreviewed block.
- Freeze fail nếu còn draft reference.
- Freeze pass tạo zip + manifest khi đủ điều kiện.

### Tests

- `test_reference_draft_stays_working`
- `test_review_requires_source`
- `test_ai_reference_requires_ai_assisted_verified`
- `test_review_promotes_to_jsonl`
- `test_lock_requires_reviewed`
- `test_freeze_blocked_by_validation`
- `test_freeze_blocked_by_unreviewed`
- `test_freeze_creates_zip_and_manifest`

## 5. Phase 5 - Polish, hardening, handoff

### Mục tiêu

Ổn định app đủ để nhóm AI-LAB dùng trong quá trình làm dataset.

### Việc cần làm

- Error handling rõ trên UI.
- Loading state cho API.
- Không mất dữ liệu nếu backend restart.
- Log đủ thao tác quan trọng.
- README chạy app.
- Test với 1 project thật ngoài golden sample.
- Hướng dẫn backup project folder.

### README cần có

```text
1. Cài requirements
2. Chạy backend
3. Mở frontend
4. Tạo project
5. Upload/extract
6. Annotate
7. Validate
8. Export/freeze
9. Cách backup
```

### Minimum run commands

Backend:

```powershell
cd app\backend
pip install -r requirements.txt
python app.py
```

Frontend prototype:

```powershell
cd app\prototype
python -m http.server 8765 --bind 127.0.0.1
```

Browser:

```text
http://127.0.0.1:8765/index.html
```

### Handoff checklist

- [ ] Backend chạy được từ clean clone.
- [ ] Frontend mở được.
- [ ] `GET /api/health` pass.
- [ ] Golden sample load được.
- [ ] Validate button gọi validator thật.
- [ ] Sửa metadata/block/glossary/entity/summary ghi file thật.
- [ ] Draft reference không vào JSONL.
- [ ] Reviewed/locked reference vào JSONL.
- [ ] Export tạo zip.
- [ ] Freeze bị chặn đúng khi chưa đủ gate.
- [ ] Freeze pass khi gate đủ.
- [ ] Không có nội dung ngoài scope AI-LAB trong handoff.

## 6. Chia việc gợi ý cho 6 người

| Người | Module | Phase chính | Output |
|---|---|---|---|
| Member 1 | Flask skeleton + routes base | Phase 1 | `app.py`, health, project routes |
| Member 2 | Dataset IO + atomic write | Phase 1-2 | read/write JSON/JSONL, patch metadata/block |
| Member 3 | Frontend API integration | Phase 1-2 | UI gọi API thay mock state |
| Member 4 | Glossary/entity/summary endpoints | Phase 2 | selection -> JSONL update |
| Member 5 | Upload/extraction | Phase 3 | TXT/EPUB extraction + jobs |
| Member 6 | Reference/export/freeze + QA | Phase 4-5 | lifecycle, zip, manifest, tests |

Lead nên review các điểm:

- Có đổi schema không? Nếu có thì chặn.
- Draft có lọt vào canonical JSONL không? Nếu có thì chặn.
- Có ghi file không atomic không? Nếu có thì sửa.
- Có endpoint nào cho phép sửa read-only field không? Nếu có thì chặn.
- Có nội dung ngoài scope AI-LAB không? Nếu có thì bỏ.

## 7. Thứ tự implement khuyến nghị

Không nên implement tất cả trong một lần. Thứ tự an toàn:

1. Phase 1: backend đọc sample + validate thật.
2. Nối UI workspace đọc API.
3. Phase 2: ghi metadata/block/review/glossary/entity/summary.
4. Nối UI autosave thật.
5. Phase 4 reference lifecycle tối thiểu.
6. Phase 3 upload/extract TXT.
7. Phase 4 export/freeze.
8. Phase 5 polish/test/handoff.

Nếu deadline gấp, MVP dùng được sớm nhất là:

```text
Phase 1 + Phase 2 + reference reviewed/locked tối thiểu
```

Upload/extract có thể làm sau nếu tooling vẫn tạo được dataset folder bằng script.

## 8. Definition of Done tổng thể

App tool được coi là usable cho AI-LAB khi:

- Mở được project dataset.
- Sửa được metadata và blocks.
- Gắn được glossary/entity bằng selection.
- Viết được chapter summary.
- Quản lý được reference draft/reviewed/locked.
- Chạy được validator JSON thật.
- Freeze gate không cho pass khi còn lỗi.
- Export/freeze tạo artifact rõ ràng.
- Tắt mở lại app không mất tiến độ.
- Dữ liệu cuối vẫn validate bằng `dataset_spec/tools/validate.py`.

