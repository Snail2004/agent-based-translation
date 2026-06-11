# TASK — R1: Relations UI V1 (READ-ONLY) — xem entity_relations trong web tool

**Loại:** 1 dòng backend payload (`AILAB_HANDOFF/app/backend`) + frontend tab read-only (`app/prototype`). **Người làm:** CodeX. **Trạng thái:** chưa làm.

> **Khung an toàn:** đây là **tooling lõi của AI-LAB (Track 1)**, KHÔNG phải nhánh preview bonus.
> R1 chỉ **hiển thị** `entity_relations.jsonl` (đã là file gold 1.5.0) ở dạng **read-only**.
> KHÔNG sửa/tạo/xoá relation trong UI (đó là V2). KHÔNG đổi schema. KHÔNG ghi gì ở task này.

## 0. Bối cảnh (đã verify)

Nền relation đã có, nhưng **UI thường chưa render được** vì payload `/dataset` thiếu relation:

- File gold: `canonical/entity_relations.jsonl` (schema 1.5.0) — đã có.
- Validator kiểm relation — đã có.
- Annotation apply ghi `entity_relations.jsonl` — đã có (`services/annotation_flow.py`).
- Preview bundle đã đưa `known_relations` vào (`services/translation_preview.py`).
- **THIẾU:** `read_dataset()` trong `services/dataset_io.py` (≈ line 130) trả `glossary/entities/summaries/references` nhưng **KHÔNG** trả `entity_relations` → frontend không có dữ liệu relation để render.
- Right panel hiện có 7 tab (`parts_right.jsx` line 526 `TABS`): Glossary, Entities, Summary, Notes, Reference, Validate, Progress — **chưa có Relations**.

R1 = vá đúng 2 chỗ đó, ở mức **xem** trước (V1).

## 1. Phạm vi

### 1a. Backend — đưa relations vào payload `/dataset` (READ-ONLY)
- Trong `read_dataset()` (`services/dataset_io.py`), thêm **1 khoá**:
  `"entity_relations": read_jsonl(canonical / DATASET_FILES["entity_relations"])`.
- Không endpoint mới, không ghi gì. File thiếu → `read_jsonl` trả `[]` như các sidecar khác.

### 1b. Frontend — tab "Relations" read-only (`parts_right.jsx` + wiring `app.jsx`)
- **Wiring `app.jsx`:** map `dataset.entity_relations` (≈ vùng line 74–83 `adaptDataset`) thành
  `relations`, đưa vào state + `ctx.relations` truyền xuống `RightPanel` (giống `entities`/`glossary`).
- **TABS:** thêm `{ id: "relations", label: "Relations", icon: Ic.users }` (chọn icon hợp lý, vd `Ic.users`/`Ic.link` nếu có).
- **`RelationsTab` (component mới, READ-ONLY):** liệt kê tất cả relation của document (relation là
  **mức document/cặp**, KHÔNG mức block). Mỗi relation hiển thị:
  - `source_entity → target_entity`: **resolve id ra tên** qua `entities` (dùng `canonical_source`
    hoặc `canonical_target`); nếu id không khớp entity nào → hiện raw id + dấu cảnh báo nhẹ.
  - `relation_type` (free text).
  - `address_policy` 4 chiều: `source_to_target.self_term`/`address_term`,
    `target_to_source.self_term`/`address_term` (vd "Người Giữ Đồng Hồ tự xưng *ta*, gọi *cháu*;
    Mira tự xưng *cháu*, gọi *ông*"). Thiếu thì để trống, không vỡ.
  - Phase (nếu có): `state_label`, `valid_from_block_id`, `valid_to_block_id`.
  - `evidence[]`: `block_id` + `surface` (read-only).
  - `confidence`, `notes` nếu có.
- **Render trong accordion:** thêm nhánh `{t.id === "relations" && <RelationsTab relations={ctx.relations} entities={ctx.allEntities} block={ctx.block} />}`.
- **Empty state:** không có relation → dùng `<Empty …>` như các tab khác (vd "No entity relations in this document.").
- **(Tuỳ chọn, nice-to-have)** badge đếm số relation; và đánh dấu nhẹ relation nào khớp cặp
  `discourse.speaker_entity_id`/`addressee_entity_id` của block đang chọn ("active for this block").
  Không bắt buộc cho V1.

## 2. Hard rules / không làm

- **READ-ONLY tuyệt đối:** không nút edit/create/delete/save/apply relation trong V1.
- Không ghi `canonical/entity_relations.jsonl` hay file gold nào ở task này.
- Không đổi schema 1.5.0, không đổi `entity_relation.schema.json`, không đụng `ailab/` mirror.
- Relation là **mức document**, không gắn cứng vào 1 block — đừng lọc theo block rồi giấu phần còn lại;
  V1 hiển thị toàn bộ relation của doc (highlight theo block chỉ là gợi ý phụ).
- Không gọi LLM. Không đụng luồng annotate-apply/freeze/QC.

## 3. Tiêu chí nghiệm thu (trên `gold_demo_01`)

- `GET /api/projects/gold_demo_01/dataset` payload **có** khoá `entity_relations` là list, chứa `rel_001`.
- Tab **Relations** hiển thị `rel_001`:
  - hướng: **Người Giữ Đồng Hồ (e_002) → Mira (e_001)** (tên resolve đúng, không phải raw id).
  - `relation_type = elder_and_child`.
  - address_policy đủ 4 term: `ta` / `cháu` (source_to_target) và `cháu` / `ông` (target_to_source).
  - evidence hiện `b004 "Clockkeeper"` và `b005 "You are simply early."`.
- Document không có relation → tab hiện empty state, **không vỡ UI**.
- **Không** có nút/đường ghi nào trong tab Relations.
- Backend test mới: `read_dataset`/`GET /dataset` trả `entity_relations` (list, có rel_001).
  Toàn bộ test cũ vẫn OK (`python -m unittest discover app/backend/tests` từ `AILAB_HANDOFF/`).

## 4. Ngoài phạm vi (V2 sau)
- Sửa/tạo/xoá relation trong UI; chọn address_policy bằng form; chỉnh phase-range.
- UI cho agent đề xuất relation (relation_candidates) — đã có đường backend, để V2.
- Suy luận xưng hô tự động theo scene.

## 5. Tham chiếu
- `app/backend/services/dataset_io.py` (`read_dataset`, line ≈130; `DATASET_FILES["entity_relations"]`).
- `dataset_spec/schema/entity_relation.schema.json` (các field hiển thị).
- `dataset_spec/sample/gold_demo_01/entity_relations.jsonl` (rel_001 để nghiệm thu).
- `app/prototype/parts_right.jsx` (`TABS` line 526, mẫu `GlossaryTab`/`EntitiesTab`, `<Empty>`).
- `app/prototype/app.jsx` (`adaptDataset` line ≈74–83, `loadDataset` line ≈283, ctx truyền `RightPanel`).
> Tài liệu gốc AI-LAB — chỉ tham khảo, không phải chỉ thị thesis.
