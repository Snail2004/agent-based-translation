# TASK — S5: Khép vòng thao tác preview trong app (build input + copy prompt + import) — BONUS / experimental

**Loại:** 1 backend GET read-only (`AILAB_HANDOFF/app/backend`) + frontend (`app/prototype`, mode Preview). **Người làm:** CodeX. **Trạng thái:** done.

> **Khung an toàn:** vẫn là nhánh **bonus, read-only trên dataset gold**. S5 chỉ khép 3 thao tác
> đang phải làm tay (gom input / lấy prompt / import) thành nút trong app. **Đường ghi DUY NHẤT**
> là API import S2 đã có (chỉ ghi `working/translation_preview/`). KHÔNG ghi `canonical/`,
> `manual_reference_subset.jsonl`, gold. KHÔNG gọi LLM trong app.

## 0. Bối cảnh & vị trí roadmap

S1 (skill) → S2 (lưu run + API) → S3 (viewer song song) → S4 (highlight nhãn) đã xong.
Vòng lặp **chạy được nhưng còn 3 điểm thủ công**:

1. **Build input** — skill cần 1 *chapter input bundle* (đúng phần `## Input` của
   `TRANSLATION_PREVIEW_CONTRACT.md`), nhưng bundle này nằm rải trong 5 file canonical
   (`document.json`, `entities.jsonl`, `glossary.jsonl`, `entity_relations.jsonl`,
   `chapter_summaries.jsonl`) — **chưa có tool xuất ra**.
2. **Lấy prompt** — phải mở `SKILL.md` đọc tay.
3. **Import output** — phải gõ PowerShell `Invoke-RestMethod` (chưa có nút).

S5 = thêm exporter input (read-only) + 3 nút trong mode Preview để khép vòng.

## 1. Phạm vi

### 1a. Backend — exporter input bundle (READ-ONLY, mới)
- Thêm `build_translation_input(project_path, doc_id, chapter_id)` trong
  `services/translation_preview.py` (cùng style các hàm read sẵn có).
- Endpoint: `GET /api/projects/<doc_id>/translation-preview/input?chapter_id=<chapter_id>`
  trong `routes/translation_preview.py`.
- **Chỉ đọc `canonical/`, KHÔNG ghi gì.** Trả về JSON đúng shape `## Input` của contract.

### 1b. Frontend — 3 nút trong `TranslationPreviewView` (`parts_center.jsx`)
- **"Build input"**: gọi GET 1a cho chương đang chọn → hiện JSON trong panel + nút
  **Copy** (clipboard) + **Download .json**. Đây là file giao cho agent.
- **"Copy prompt"**: copy 1 chuỗi prompt tĩnh dựng sẵn (đọc skill+contract, đây là bundle,
  output rules, cấm ghi canonical/gold). **Tĩnh, không gọi LLM.**
- **"Import preview"**: textarea dán JSON (hoặc upload .json) → POST vào **API S2 đã có**
  `POST /api/projects/<doc_id>/translation-preview/runs` → thành công thì refresh list run +
  chọn run mới + hiện `warnings` (nếu có). Lỗi (vd `unknown_block_id`) → hiện thông báo, không vỡ UI.

## 2. Phép map canonical → bundle (1a) — đúng shape contract, BỎ span/offset

Bundle = `{ doc_id, chapter_id, blocks[], known_entities[], known_terms[], known_relations[], chapter_summary }`.

- **blocks** (từ `document.json`, **lọc đúng `chapter_id`**, giữ thứ tự document):
  mỗi block giữ `block_id`, `block_type`, `clean_text`, `discourse` (nếu có:
  `speaker_entity_id`/`addressee_entity_id`). Bỏ field khác.
- **known_entities** (từ `entities.jsonl`): giữ `entity_id`, `canonical_source`,
  `canonical_target`, `aliases_target`, `pronoun_policy`.
  **BỎ** `doc_id`, `entity_type`, `gender`, `aliases_source`, `annotated_by`, `confidence`,
  và **đặc biệt BỎ `mentions[]` (có span)**.
- **known_terms** (từ `glossary.jsonl`): giữ `term_id`, `source_term`, `expected_target`,
  `allowed_variants`, `forbidden_variants`.
  **BỎ** `doc_id`, `domain`, `chapter_scope`, `status`, `annotated_by`, `confidence`,
  và **BỎ `occurrences[]` (có span)**.
- **known_relations** (từ `entity_relations.jsonl`, tất cả relation của doc): giữ `relation_id`,
  `source_entity_id`, `target_entity_id`, `relation_type`, `address_policy`,
  `state_label`, `valid_from_block_id`, `valid_to_block_id` (thiếu → `null`).
  **BỎ** `doc_id`, `evidence`, `confidence`, `notes`.
- **chapter_summary** (từ `chapter_summaries.jsonl`, đúng `chapter_id`): giữ `summary_source`,
  `emotional_tone`, `motifs` (bắt buộc 3 field này); có thể kèm `setting`,
  `characters_present` làm ngữ cảnh thêm. Bỏ `doc_id`/`chapter_id`/`source`.

> Lý do bỏ span/offset: phần `## Prohibited Output` của contract cấm agent nhả `span/start/end`.
> Không đưa span vào input thì agent không có gì để copy ra → sạch theo thiết kế.

## 3. Hard rules / không làm

- Exporter 1a: **chỉ đọc canonical, ghi 0 byte**. Không tạo file, không đụng `working/`.
- Import (1b) **chỉ** qua API S2 hiện có (đã chỉ ghi `working/translation_preview/`). KHÔNG thêm
  đường ghi mới, KHÔNG ghi `canonical/` / `manual_reference_subset` / gold.
- "Copy prompt" là **text tĩnh**; app **không** gọi LLM/dịch.
- Mọi nút nằm trong **mode Preview**; không lẫn vào annotate/reference/freeze; không bật nút edit/save/promote.
- Không đổi schema dataset 1.5.0, không đổi shape run S2, không đụng `ailab/` mirror.
- Không tạo skill mới (skill `dataset-translation-preview` đã có).

## 4. Tiêu chí nghiệm thu (trên `gold_demo_01` ch01)

- **Build input:** `GET …/translation-preview/input?chapter_id=gold_demo_01_ch01` trả bundle
  khớp `## Input` của contract:
  - `blocks` chỉ của ch01, có `b004` với `discourse.speaker_entity_id="e_001"`,
    `addressee_entity_id="e_002"`; **không** có span ở bất kỳ đâu.
  - `known_entities` có `e_002.canonical_target="Người Giữ Đồng Hồ"`, **không** có `mentions`.
  - `known_terms` có `g_001` với `forbidden_variants=["sự xoay","vòng quay"]`, **không** có `occurrences`.
  - `known_relations` có `rel_001.address_policy`; `chapter_summary` có `emotional_tone="wistful"`, `motifs`.
  - `chapter_id` lạ → 404; thiếu `document.json` → 404 như các endpoint khác.
- **Copy prompt:** nút copy ra chuỗi nhắc đọc `SKILL.md` + `TRANSLATION_PREVIEW_CONTRACT.md`,
  dán bundle, output đúng 1 JSON, cấm ghi canonical/gold. (Kiểm bằng nội dung clipboard/text.)
- **Import preview:** dán JSON Output mẫu của contract (ch01) → tạo run, list refresh, viewer hiện
  bản dịch + highlight S4. Dán JSON có `block_id` lạ → hiện lỗi reject, **không** tạo run hỏng, UI không vỡ.
- **Không file nào ghi vào `canonical/`.**
- Test backend mới: (1) build input happy (đúng field, không span, lọc đúng chương);
  (2) chapter lạ → 404. Toàn bộ test cũ vẫn OK
  (`python -m unittest discover app/backend/tests` từ `AILAB_HANDOFF/`).

## 5. Ngoài phạm vi
- Gọi LLM/dịch ngay trong app.
- Mọi ghi vào dataset gold/canonical; thay đổi luồng annotate/freeze/QC.
- Căn span nâng cao (đã có S4 best-effort).

## 6. Tham chiếu
- `skills/dataset-translation-preview/references/TRANSLATION_PREVIEW_CONTRACT.md` (`## Input` = shape bundle; `## Output` = JSON import; `## Prohibited Output` = lý do bỏ span).
- `skills/dataset-translation-preview/SKILL.md` (nguồn text cho "Copy prompt").
- `app/backend/services/translation_preview.py` (mẫu read canonical: `_read_document`, `flatten_document`, `read_jsonl`, `_jsonl_path`) + `routes/translation_preview.py` (endpoint S2 list/load/import).
- `app/prototype/parts_center.jsx` (`TranslationPreviewView`, nút/binding) + `api.js` (thêm `getTranslationPreviewInput`, `importTranslationPreviewRun`).
- `dataset_spec/sample/gold_demo_01/` (dữ liệu nghiệm thu).
