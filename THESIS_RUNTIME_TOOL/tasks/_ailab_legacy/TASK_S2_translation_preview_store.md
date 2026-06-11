# TASK — S2: Lưu translation-preview run (BONUS / experimental)

**Loại:** backend `AILAB_HANDOFF/app/backend` (storage). **Người làm:** CodeX. **Trạng thái:** đã làm + verify.

> **Khung an toàn:** vẫn là nhánh **bonus/thử nghiệm, read-only trên dataset**. S2 chỉ lưu
> output preview của S1 vào **vùng working (KHÔNG canonical, KHÔNG gold)**. Tuyệt đối không
> ghi `canonical/`, `manual_reference_subset.jsonl`. Đây không phải dữ liệu dataset.

## 0. Bối cảnh & vị trí roadmap

S1 (xong) cho skill nhả **JSON bản dịch preview per-block** (`target_text` + `mentions` +
`address_applied` + `used_context`). S2 = **lưu lại run đó** để (a) tồn tại sau khi đóng,
(b) so nhiều lần dịch, (c) làm nguồn cho S3 (app song song) đọc.

- S2 (task này): import + validate + lưu + load preview run. **Không UI** (S3), **không căn span** (S4).
- Ánh xạ khái niệm theo `../RUN_EVAL_SCHEMA.md`: `target_text` per block ≡ `translation_runs`;
  `used_context` per block ≡ `context_bundle.memory_refs`. (App dùng JSON/JSONL, không DB.)

## 1. Phạm vi (trong `AILAB_HANDOFF/app/backend`)

- `services/`: thêm module lưu/đọc preview run (mirror cách annotation/normalizer flow lưu working artifacts).
- `routes/`: endpoint import (POST) + list + load (GET) preview run.
- Storage: dưới **`<project>/working/translation_preview/`** (vùng working, KHÔNG canonical):
  - `runs/<run_id>.json` — full output 1 chương + metadata run.
  - `index.json` — danh sách run (run_id, chapter_id, created_at, model/skill_version, counts).
- Tests trong `app/backend/tests`.

Không tạo skill mới. Không đụng `ailab/` mirror. Không đổi schema dataset 1.5.0.

## 2. Shape run lưu (`runs/<run_id>.json`)

```json
{
  "run_id": "tpreview_ch01_001",
  "doc_id": "gold_demo_01",
  "chapter_id": "gold_demo_01_ch01",
  "kind": "translation_preview",
  "model": "optional",
  "skill_version": "dataset-translation-preview@1",
  "prompt_version": "optional",
  "created_at": "ISO-8601",
  "blocks": [
    {
      "block_id": "gold_demo_01_ch01_b004",
      "target_text": "...",
      "mentions": [{"entity_id|term_id": "...", "source_surface": "...", "target_surface": "..."}],
      "address_applied": {"pair": "e_001->e_002", "self_term": "cháu", "address_term": "ông", "relation_id": "rel_001"},
      "used_context": ["e_001", "e_002", "rel_001", "gold_demo_01_ch01"]
    }
  ]
}
```
- `run_id`: sinh ổn định, không trùng (vd `tpreview_<chapterShort>_<NN>` hoặc timestamp).
- Giữ nguyên block theo `block_id` (trục align bất biến cho S3/S4).

## 3. Validate khi import (referential, không phải schema dataset)

Trước khi lưu, kiểm trên dataset **đã có** của project:
- mỗi `block_id` trong preview ∈ `document.json` block ids; lạ → **reject** (hoặc flag, không lưu block đó).
- mỗi id trong `used_context` ∈ `{entity_ids ∪ term_ids ∪ relation_ids ∪ chapter_ids}`; lạ → **warning** (chống agent bịa id).
- `address_applied.relation_id` (nếu có) ∈ relation ids.
- `doc_id`/`chapter_id` khớp project.
- **Không** validate `target_text`/chất lượng dịch (ngoài phạm vi).

## 4. Hard rules / không làm

- **Không ghi** `canonical/`, `manual_reference_subset.jsonl`, hay bất kỳ file dataset gold.
- Lưu ở `working/translation_preview/` → coi như **working artifact**; nên **gitignore** giống `working/autosave/`/`logs/` (không commit output preview).
- Preview run **không** là input cho freeze/QC dataset.
- Cho phép **nhiều run cùng 1 chương** (so sánh) — không ghi đè trừ khi cùng `run_id`.

## 5. Tiêu chí nghiệm thu

- POST import một preview JSON hợp lệ (vd output S1 cho `gold_demo_01` ch01) → tạo
  `working/translation_preview/runs/<run_id>.json` + cập nhật `index.json`.
- GET list trả về run vừa tạo; GET load `<run_id>` trả về per-block `target_text` + `used_context` + `mentions`.
- Import preview có `block_id` lạ → **reject/flag**, không tạo run hỏng.
- Import preview có `used_context` id lạ → **warning** trong kết quả import.
- **Không file nào ghi vào `canonical/`.**
- Tests mới: (1) import+load happy; (2) bad block_id; (3) unknown used_context id → warning.
- Toàn bộ test cũ vẫn OK (`python -m unittest discover app/backend/tests` từ `AILAB_HANDOFF/`).

## 6. Ngoài phạm vi (task sau)
- S3 app song song trái EN/phải VI (render từ run đã lưu).
- S4 căn nhãn entity/term theo `target_surface` (best-effort span trong `target_text`).
- Mọi ghi vào dataset gold.

## 7. Tham chiếu
- `../RUN_EVAL_SCHEMA.md` (translation_runs / context_bundle / block_id là trục align).
- `skills/dataset-translation-preview/references/TRANSLATION_PREVIEW_CONTRACT.md` (shape input/output S1).
- `app/backend/services/annotation_flow.py` (mẫu lưu working artifact + referential check).
- `dataset_spec/sample/gold_demo_01/` (dataset để validate referential khi import).
> Tài liệu gốc AI-LAB — chỉ tham khảo, không phải chỉ thị thesis.
