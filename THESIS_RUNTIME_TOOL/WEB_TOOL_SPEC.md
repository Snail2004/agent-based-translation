# WEB_TOOL_SPEC — AILAB Dataset Tool (AIL-202)

> **Phạm vi:** Đặc tả app **xây dựng dataset** EN→VI văn bản dài. **KHÔNG phải app dịch, KHÔNG phải hệ agent, KHÔNG phải PDF-layout tool.** App đưa nhóm đi từ **file nguồn EN → 5 file dataset JSON/JSONL sạch (validate pass)**.
> Liên quan: schema **1.5.0** (`dataset/schema/`), `dataset/tools/validate.py`, `WORKFLOW.md`, `DATASET_DESIGN.md`, `AILAB_PLAN.md`.
> Mô hình: **một app end-to-end duy nhất**, chạy **local**, dữ liệu phối hợp qua **git** (mỗi người 1 `doc_id` một lúc). Một codebase, chia việc theo module.

---

## 1. Mục tiêu & Non-goals

### 1.1. Mục tiêu
Một app local: Upload nguồn → Extract → Review/clean block → Annotate (glossary/entity/summary/reference) → Validate → Export/Freeze. Giữ tiến độ khi tắt/mở lại. Không bao giờ để draft chưa duyệt lẫn vào file canonical.

### 1.2. Non-goals (KHÔNG làm — kể cả stretch)
- Không phải app dịch tự động toàn bộ tác phẩm; chỉ hỗ trợ nhập/review reference VI theo năng lực nhóm.
- Không OCR, không PDF layout preservation, không WYSIWYG PDF editor.
- Không parse PDF/EPUB phức tạp bằng JS trong browser (extraction là backend Python).
- Không realtime multi-user, không login/auth, không database server bắt buộc, không worker queue (Redis/Celery).
- Không cho sửa qua UI: `source_text`, `block_id`, `chapter_id`, `order_index`, `raw_sha256`, `provenance` do pipeline sinh.

---

## 2. Kiến trúc

```
AILAB Dataset Tool (local)
├── frontend/  (vanilla HTML/CSS/JS — thỏa yêu cầu "ngôn ngữ web cơ bản")
│     upload · block browser · editors · validator view · progress · export
└── backend/   (Python — Flask; thỏa "Python pipeline")
      extraction · save/load project · validate.py wrapper · export/freeze · logs
```
- **Frontend** chỉ là giao diện thao tác + tính span từ text-selection. **Mọi logic nặng (extraction, validate, IO) ở backend.**
- **Backend** chạy localhost (vd `http://127.0.0.1:5000`); frontend gọi REST JSON.
- **Không** nhúng extraction vào browser. Nút "Extract" = gọi `POST /extract` → backend chạy pipeline.
- Single-user/máy. Phối hợp nhóm = **git** trên thư mục `ailab_projects/`.

---

## 3. Project workspace (filesystem)

Mỗi nguồn = 1 project theo `doc_id`:
```
ailab_projects/
  <doc_id>/
    project.json              # metadata project + current user + trạng thái chung
    raw/
      source.<ext>            # file nguồn gốc (immutable)
      provenance.json         # source_url, license, retrieved_at, raw_sha256, source_format
    canonical/                # 5 FILE DATASET (live, structurally valid) — vào git
      document.json
      glossary.jsonl
      entities.jsonl
      chapter_summaries.jsonl
      manual_reference_subset.jsonl
    working/                  # trạng thái làm việc — review_state + CSV vào git; autosave KHÔNG
      review_state.json       # block/summary/reference nào đã review, ai review, ghi chú
      drafts.json             # source of truth cho reference draft trong app
      translation_review_log.csv   # CSV export read-only từ drafts.json để xem/QC
      autosave/               # snapshot phục hồi crash (gitignore)
        document.autosave.json
        glossary.autosave.jsonl
        ...
    logs/                     # gitignore
      extraction.log
      validation.log
      app_events.jsonl        # ai sửa field nào, lúc nào
      jobs.jsonl              # local job record (extract...)
    exports/                  # gitignore
      dataset_<doc_id>_v0.1.0.zip
    CHANGELOG.md              # changelog riêng của project dataset, KHÔNG phải schema changelog
    versions.json             # sha256/version cho snapshot dataset của project
```

**Ranh giới git:** vào git = `project.json`, `raw/` (+ provenance nếu license/file size cho phép), `canonical/`, `working/review_state.json`, `working/drafts.json`, `working/translation_review_log.csv`, project `CHANGELOG.md`, `versions.json`. **gitignore** = `working/autosave/`, `logs/`, `exports/`. Nếu raw file quá lớn hoặc license không cho commit, chỉ commit `raw/provenance.json` + `raw_sha256` + hướng dẫn lấy lại nguồn; không commit file raw.

---

## 4. Data model: Canonical vs Working

### 4.1. Quy tắc cốt lõi (đọc kỹ — chống over-engineer)
| Loại | Sửa ở đâu | Promotion? |
|---|---|---|
| `document.json`, `glossary.jsonl`, `entities.jsonl`, `chapter_summaries.jsonl` | **Sửa thẳng vào `canonical/`** (annotation nhóm sở hữu) | ❌ Không có draft→promote; autosave chỉ để phục hồi crash |
| `manual_reference_subset.jsonl` | Draft ở `working/drafts.json` (CSV là export read-only) | ✅ **CHỈ reference**: draft → `reviewed` mới promote sang JSONL |
| Trạng thái review/tiến độ | `working/review_state.json` | — (KHÔNG nhồi vào schema) |

→ **"Freeze"** = `validate.py` PASS **+** mọi block/summary `reviewed` (theo `review_state.json`) **+** mọi reference `reviewed/locked` → **zip + tag version** vào `exports/` + ghi CHANGELOG. Freeze là **snapshot**, không phải copy working→canonical.

**Canonical không chứa record dở dang.** UI có thể có form đang nhập dở, nhưng backend chỉ ghi một record vào `canonical/` khi record đó đạt schema-minimal:
- glossary có đủ `term_id, doc_id, source_term, expected_target, status, occurrences >= 1`;
- entity có đủ `entity_id, doc_id, canonical_source, canonical_target, entity_type, mentions >= 1`;
- chapter summary có đủ `doc_id, chapter_id, summary_source, source`;
- reference canonical chỉ nhận entry `reviewed|locked`.

Form chưa đủ field nằm trong UI state/autosave hoặc working draft, không ghi vào JSONL canonical. Điều này giữ `canonical/` luôn có thể validate được theo schema hiện tại.

### 4.2. Phân quyền field (enforce trong UI)
- **Read-only (pipeline sinh):** `doc_id`, `chapter_id`, `block_id`, `order_index`, `source_text`, `raw_sha256`, `source_url`, `extraction_tool`, `pipeline_version`, `provenance.*`.
- **Editable (annotator):** `clean_text`, `block_type`, `is_chapter_opening`, `quality_flags`, `discourse.*`, `annotations.*`; metadata mô tả (`title/author/genre/domain`); glossary/entities/chapter_summaries; reference draft.
- **Chỉ Reviewer/Lead:** `metadata.license`, `metadata.contamination_risk`; promote glossary `status` → `locked/human_verified`; **đánh dấu `reviewed`/`locked`**; freeze.

### 4.3. Annotation write consistency

Khi tag glossary/entity bằng text-selection, app phải giữ đồng bộ hai lớp:

- sidecar authoritative:
  - glossary: `glossary.occurrences[] = {block_id, span}`;
  - entity: `entities.mentions[] = {block_id, surface, span}`;
- pointer trong block:
  - `block.annotations.term_occurrences[]` chứa `term_id`;
  - `block.annotations.entity_mentions[]` chứa `entity_id`.

Quy tắc implement: hoặc backend cập nhật **cả hai nơi trong cùng một transaction/save**, hoặc sidecar là nguồn chính rồi backend regenerate block annotation pointers trước khi save/export. Không để tình trạng sidecar có occurrence/mention nhưng block thiếu pointer, vì các bước tra cứu hoặc kiểm tra dữ liệu về sau sẽ dễ bỏ sót dữ liệu dù file vẫn có thể validate.

### 4.4. `review_state.json` (shape gợi ý)
```json
{
  "doc_id": "...",
  "blocks": { "<block_id>": {"reviewed": true, "reviewed_by": "U2", "flags": [], "note": ""} },
  "chapter_summaries": { "<chapter_id>": {"reviewed": true, "reviewed_by": "U2"} },
  "references": { "<reference_id>": {"status": "reviewed", "reviewed_by": "U2"} },
  "iaa_overlap_blocks": ["..."],
  "updated_at": "..."
}
```

---

## 5. Backend API (REST JSON)

| Method · Endpoint | Mục đích |
|---|---|
| `GET /projects` | Liệt kê project (đọc `ailab_projects/*/project.json`) |
| `POST /projects` | Tạo project: `{doc_id, metadata}` → tạo workspace |
| `GET /projects/{doc_id}` | Load toàn bộ (canonical + working + progress) |
| `POST /projects/{doc_id}/source` | Upload file nguồn → lưu `raw/`, tính `raw_sha256`, set `source_format`/provenance |
| `POST /projects/{doc_id}/extract` | Chạy extraction → `document.json` draft + `extraction.log` + job record; trả structured result |
| `GET /projects/{doc_id}/document` | Lấy `document.json` |
| `PUT /projects/{doc_id}/blocks/{block_id}` | Sửa field editable của block (server từ chối field read-only) |
| `GET/POST/PUT/DELETE /projects/{doc_id}/glossary[/{term_id}]` | CRUD glossary |
| `GET/POST/PUT/DELETE /projects/{doc_id}/entities[/{entity_id}]` | CRUD entity |
| `GET/PUT /projects/{doc_id}/summaries/{chapter_id}` | Chapter summary |
| `GET/POST/PUT /projects/{doc_id}/refs` | Reference draft (ghi vào CSV working) |
| `POST /projects/{doc_id}/refs/{reference_id}/promote` | Promote reference draft → JSONL (chỉ khi đủ field + status reviewed) |
| `POST /projects/{doc_id}/validate` | Gọi `validate.py --json` → trả mảng lỗi `{file, block_id, location, message}` |
| `GET /projects/{doc_id}/progress` | Thống kê tiến độ từ `review_state.json` |
| `POST /projects/{doc_id}/autosave` | Ghi snapshot atomic vào `working/autosave/` |
| `POST /projects/{doc_id}/save` | Ghi canonical + working (atomic) |
| `POST /projects/{doc_id}/export` | Export canonical (zip) |
| `POST /projects/{doc_id}/freeze` | Kiểm điều kiện freeze → zip + version + CHANGELOG |
| `GET /projects/{doc_id}/logs` | Đọc logs/jobs |
| `GET/POST /user` | Lấy/đặt **current user name** (không auth) |

**Yêu cầu validate.py `--json`:** validator hiện in text → cần thêm chế độ `--json` (mảng `{file, location, block_id?, message, severity}`) để app parse + nhảy tới lỗi. *(Thay đổi nhỏ ở tooling, không đổi schema/version.)*

---

## 6. Modules (chức năng + UI + edge cases)

### M1. Project / Source Manager
- **Tạo project** theo `doc_id` (validate `doc_id` đúng convention, chưa tồn tại).
- **Metadata nguồn:** `title, author, domain, genre, source_format, license, source_url, contamination_risk` (license/contamination_risk chỉ Lead/Reviewer sửa).
- **Upload nguồn:** ưu tiên `.txt`, `.epub`; `.pdf` born-digital = stretch. Lưu `raw/`, tính `raw_sha256`, set `source_format`.
- **Hiển thị provenance** (read-only sau khi set).
- **Open/Resume project**, **list project**.
- *Edge:* `doc_id` trùng → từ chối; format không hỗ trợ → báo lỗi rõ; thiếu license → cảnh báo (không chặn tạo, nhưng chặn freeze).

### M2. Extraction
- Nút **Extract** → `POST /extract` (backend Python).
- Backend tự chia **chapter / heading / paragraph / dialogue (nếu phát hiện) / footnote (nếu có)**; sinh `chapter_id, block_id, order_index, source_text, clean_text(=source_text chuẩn hóa), block_type, is_chapter_opening, quality_flags`.
- Chạy `validate.py` sơ bộ sau extraction; ghi `extraction.log` + job record.
- *Edge:* extraction ra **0 block** → báo lỗi, không tạo document rỗng; file hỏng/encoding lạ → flag + thông báo; chương không nhận diện được → để cả file là 1 chapter + cảnh báo; chạy lâu → job record `status: running/done/failed`, frontend poll `GET /logs`.

### M3. Chapter / Block Browser
- Cây **chapter → block**; chọn block để edit.
- **Filter:** chưa review · có `quality_flags` · `dialogue` · `is_chapter_opening` · có annotation · span nghi ngờ.
- Hiển thị song song **`source_text` (read-only)** ↔ **`clean_text` (editable)**.
- *Edge:* document rất dài → phân trang/lazy-load theo chapter.

### M4. Block Review / Cleaning Editor
- Sửa `clean_text`; chọn `block_type ∈ {heading, paragraph, dialogue, footnote}`; tick `is_chapter_opening`; gắn `quality_flags` (controlled vocab: `ok, needs_review, extraction_error, ocr_suspect, broken_paragraph, unclear_dialogue, license_check_needed`).
- `quality_flags`: nếu có `ok` thì không đi cùng flag lỗi khác. Khi có bất kỳ flag khác, bỏ `ok`.
- Khóa: `source_text, block_id, order_index, raw_sha256, provenance`.
- Đánh dấu **block reviewed** (ghi `review_state` + `reviewed_by` = current user).
- *Edge quan trọng — **sửa `clean_text` làm hỏng span**:* nếu block đã có glossary occurrence / entity mention, khi `clean_text` đổi, app phải **re-check span** (kiểm substring tại `[start,end]` còn khớp không); nếu lệch → gắn cờ `needs_review` cho các annotation đó + cảnh báo "đã sửa text, hãy re-tag N annotation". Không tự xóa, để người quyết.
- Text trước khi tính/lưu span phải dùng cùng normalization (khuyến nghị Unicode NFC) ở frontend và backend; backend re-check substring sau khi lưu để tránh lệch offset do khác normalization.
- *Edge:* đổi `block_type` ra/khỏi `dialogue` → bật/ẩn ô `discourse` (speaker/addressee).

### M5. Glossary Editor
- CRUD term: `source_term, expected_target, allowed_variants[], forbidden_variants[], chapter_scope, status, confidence`.
- **Gắn occurrence bằng bôi đen text trong `clean_text`** → frontend tính `[start,end]` từ selection (không cho nhập span tay nếu tránh được). Lưu `{block_id, span}`.
- Hiển thị term xuất hiện ở block nào; cảnh báo nếu `status=locked` nhưng đưa từ văn phong thường vào (nhắc nguyên tắc "glossary chỉ cho term bất biến").
- *Edge:* xóa term còn được `block.annotations.term_occurrences` trỏ tới → **chặn xóa hoặc cascade-warn** (validator sẽ fail dangling ref); `term_id` trùng → từ chối; `occurrences` rỗng → cảnh báo (schema yêu cầu ≥1).

### M6. Entity Editor
- CRUD entity: `canonical_source, canonical_target, entity_type, gender, aliases_source[], aliases_target[], pronoun_policy`.
- Gắn **mention bằng text-selection** → `{block_id, surface, span}`.
- **Discourse cho dialogue:** chọn `speaker_entity_id`, `addressee_entity_id`, `pronoun_hints`.
- *Edge:* xóa entity còn được trỏ (`entity_mentions`, `discourse.speaker/addressee`, `chapter_summaries.characters_present`) → chặn/cascade-warn; `mentions` rỗng → cảnh báo; nhắc nguyên tắc **consistency ≠ verbatim** (entity là danh tính, alias/pronoun được phép — không phải "thay mọi mention").

### M7. Chapter Summary Editor
- Mỗi chương: **bắt buộc** `summary_source` + `source ∈ {human, ai_assisted_verified}`. Optional: `characters_present[] (entity_id), setting, emotional_tone, motifs[], key_events[], open_threads[], translation_notes, summary_target, confidence`.
- **Nhắc viết summary ngay sau khi review xong chương** (UI prompt khi đánh dấu chapter reviewed).
- *Edge:* `characters_present` trỏ entity không tồn tại → chặn (validator fail); freeze chỉ nhận `source` đã verify (không có giá trị draft).

### M8. Reference Subset / Translation Review
- Chọn block làm reference; gán `stratum ∈ {chapter_opening, dialogue, narrative, term_heavy, random}`.
- Dịch `reference_vi`; ghi `source, status, translated_by, reviewed_by`, và nếu AI tham gia: `ai_model, prompt_id, ai_used_at`.
- **Lifecycle:** draft sửa qua UI/API và ghi vào `working/drafts.json`; `working/translation_review_log.csv` chỉ là export read-only sinh tự động để xem/QC; **chỉ `reviewed/locked` mới `POST /promote` sang `manual_reference_subset.jsonl`**.
- *Edge:* promote khi thiếu `source/status` hoặc `status=draft` → chặn; `reference_translation_id` trên block phải trỏ đúng reference cùng block (validator kiểm); raw AI output chưa người sửa → cấm promote.

### M9. Working State / Progress Dashboard
- Đọc `review_state.json`: % block reviewed/total, #glossary, #entities, #summaries, #references reviewed/locked, #block có cờ, **#lỗi validation còn lại**.
- *Edge:* dashboard là **read từ working state**, không ghi gì vào schema.

### M10. Validation View
- Nút **Validate** → `POST /validate` (backend chạy `validate.py --json`).
- Hiển thị lỗi theo **file / block / field**, có **link nhảy tới** block/record.
- *Edge:* lỗi referential (dangling ref) → link tới cả 2 phía; lỗi structural → chỉ field; validate fail → **chặn freeze**.

### M11. Export / Freeze
- **Export canonical:** zip `<doc_id>/` chỉ gồm 5 file (không kèm working/logs).
- **Freeze:** kiểm `validate.py` PASS **+** mọi block/summary reviewed **+** reference reviewed/locked **+** provenance/license đủ → tạo `exports/dataset_<doc_id>_vX.Y.Z.zip` + **ghi project `CHANGELOG.md` entry + cập nhật project `versions.json` (sha256)**. Không ghi vào `ailab/dataset/CHANGELOG.md` của schema spec.
- **Export working state riêng** (review_state + CSV) cho backup.
- *Edge:* thiếu điều kiện → freeze bị chặn, liệt kê việc còn thiếu; không bao giờ đưa reference draft vào bản freeze.

---

## 7. Cross-cutting

### 7.1. Current user (không auth)
1 ô **tên người dùng** (lưu `project.json`/localStorage). Mọi hành động ghi `reviewed_by/translated_by/corrected_by/annotated_by` + `app_events.jsonl` theo tên này. Cần cho **4-eyes + IAA + provenance**.

### 7.2. Autosave (atomic — chống hỏng file)
- Tự lưu sau mỗi edit/blur hoặc định kỳ (vd 30s) vào `working/autosave/`.
- **Ghi atomic:** ghi temp → `os.replace` (rename) → tránh file hỏng nếu crash giữa chừng.
- Mở lại project: nếu autosave **mới hơn** canonical → hỏi "khôi phục bản chưa lưu?".

### 7.3. History / Logs / Resume
- `app_events.jsonl`: `{ts, user, action, target, field, old?, new?}` — đủ truy vết, không cần audit enterprise.
- `extraction.log`, `validation.log`, `jobs.jsonl` (job record tối giản như §dưới).
- **Create / Open / Save / Resume** project: mở lại đọc `canonical/` + `working/` → khôi phục đúng tiến độ.

### 7.4. Job record tối giản (extraction lâu)
```json
{"job_id":"extract_2026_06_01_153000","type":"extract","status":"done",
 "input_file":"raw/source.epub","started_at":"...","finished_at":"...",
 "output":"canonical/document.json","log_file":"logs/extraction.log"}
```
Local file-based, **không** worker queue/DB.

---

## 8. Edge cases tổng hợp (checklist)
- [ ] Sửa `clean_text` → re-check span occurrence/mention; lệch thì cờ `needs_review` + cảnh báo.
- [ ] Tính span sau Unicode NFC normalization; backend re-check substring tại `[start,end]`.
- [ ] Xóa term/entity còn được trỏ → chặn hoặc cascade-warn (tránh dangling ref).
- [ ] Tag glossary/entity cập nhật đồng bộ sidecar occurrence/mention và block annotation pointer.
- [ ] Không ghi record dở dang vào `canonical/`; form chưa đủ field chỉ nằm ở UI/autosave/working.
- [ ] `block_type` đổi sang/khỏi dialogue → bật/ẩn discourse.
- [ ] Extraction 0 block / file hỏng / encoding lạ → báo lỗi, không tạo document rỗng.
- [ ] Promote reference thiếu field hoặc còn draft → chặn.
- [ ] Reference `reference_translation_id` ↔ block không khớp → cảnh báo.
- [ ] `doc_id`/`term_id`/`entity_id`/`reference_id` trùng → từ chối.
- [ ] App crash giữa lúc ghi → autosave atomic khôi phục.
- [ ] Hai người sửa cùng `doc_id` → ngoài app, giải bằng git + quy ước 1 doc/người.
- [ ] UTF-8 toàn bộ (đọc/ghi); không mojibake.
- [ ] Validate fail → chặn freeze + liệt kê việc thiếu.
- [ ] Sửa field read-only qua API → backend từ chối (HTTP 400).
- [ ] Project freeze ghi project `CHANGELOG.md`/`versions.json`, không ghi nhầm schema changelog.
- [ ] File lớn / document dài → lazy-load theo chapter, autosave không block UI.

---

## 9. MVP (6 module bắt buộc) vs Stretch
**MVP bắt buộc:**
1. Project + upload (M1)
2. Extraction TXT/EPUB → document.json (M2)
3. Block browser + clean editor (M3+M4)
4. Glossary + Entity editor bằng text-selection (M5+M6)
5. Summary + Reference editor (M7+M8)
6. Validate + Export/Freeze (M10+M11)
**Stretch (nếu kịp):** PDF born-digital extraction; Progress dashboard (M9) đầy đủ; jump-to-error nâng cao; AI draft helper (để sau cùng).
**Không làm:** xem §1.2.

---

## 10. Chia module cho 6 người (một codebase)
| # | Mảng | Module |
|---|---|---|
| 1 | Backend extraction + project save/load + sha256/provenance | M1(BE), M2 |
| 2 | Backend validate (`--json`) + export/freeze + versioning | M10(BE), M11(BE) |
| 3 | Frontend project manager + source upload + block browser/filter | M1(FE), M3 |
| 4 | Frontend block editor + hạ tầng span text-selection | M4 + selection infra |
| 5 | Frontend glossary + entity editor | M5, M6 |
| 6 | Frontend summary + reference editor + progress + test golden sample | M7, M8, M9 |
Tất cả trên **một repo/folder, một format dữ liệu, một backend**. Cross-cutting (current user, autosave, logs) thống nhất ở backend + 1 module FE chung.

---

## 11. Tech stack & setup
- **Backend:** Python + **Flask** (đơn giản hơn FastAPI cho nhóm), gọi `validate.py` qua subprocess (`--json`); extraction dùng `ebooklib`+`BeautifulSoup` (EPUB), parser TXT, (stretch) PyMuPDF cho PDF born-digital.
- **Frontend:** **vanilla HTML/CSS/JS** (thỏa yêu cầu "ngôn ngữ web cơ bản"); fetch REST; không framework nặng.
- **Run:** `pip install -r requirements.txt` → `python backend/server.py` → mở `http://127.0.0.1:5000`.
- **Data:** `ailab_projects/` trong git (theo ranh giới §3).

---

## 12. Definition of Done (cho app)
- [ ] Tạo project + upload TXT/EPUB → extract → `document.json` validate pass.
- [ ] Sửa block (clean_text/type/flags), span qua text-selection, không sửa được field read-only.
- [ ] Glossary/Entity/Summary/Reference editor hoạt động + ghi đúng canonical/working.
- [ ] Glossary/entity tag bằng selection ghi đồng bộ sidecar + block annotations; canonical không chứa record dở dang.
- [ ] Reference draft ở CSV, promote reviewed → JSONL; không draft nào lọt canonical.
- [ ] Validate view chạy `validate.py --json`, hiện lỗi + nhảy tới block.
- [ ] Export 5 file đúng layout; Freeze chỉ khi đủ điều kiện + ghi project CHANGELOG/versions.
- [ ] Tắt/mở app vẫn còn tiến độ (autosave atomic + resume).
- [ ] Chạy được trọn vẹn trên **golden sample `gold_demo_01`** (import → hiển thị → validate PASS → export).
- [ ] `app_events.jsonl` ghi ai-sửa-gì-khi-nào.

---

## 13. Out of scope (nhắc lại để khỏi phình)
App này **chỉ phục vụ dataset construction**. Extraction là **backend Python deterministic**, không phải browser-extraction, không phải PDF-layout pipeline. Không dịch, không OCR, không layout, không multi-user/auth/DB. Mọi thứ về hệ thống dịch tự động nâng cao hoặc PDF layout là **scope khác**, không đưa vào đây.

---
*Spec cho AILAB Dataset Tool. Schema 1.5.0. Một app end-to-end, local + git, không phải app dịch.*
