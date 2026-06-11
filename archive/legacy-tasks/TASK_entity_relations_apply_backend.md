# TASK — Backend resolve/apply cho `entity_relations` (schema 1.5.0)

**Loại:** code backend AI-LAB tool (`AILAB_HANDOFF/app/backend`). **Không đổi schema 1.5.0** (đã khóa).
**Người làm:** CodeX. **Trạng thái:** chưa làm.

---

## 0. Bối cảnh & gap

- Schema 1.5.0 đã có sidecar `entity_relations.jsonl` + `entity_relation.schema.json` + validator (referential + overlap) — **đã xong, validate PASS**.
- Skill `dataset-annotation-drafter` đã emit **`relation_candidates`** (contract + SKILL.md cập nhật rồi).
- **GAP:** backend chưa resolve/apply quan hệ. Đã verify: `grep "relation|entity_relations|address_policy"` trong `app/backend/services/annotation_flow.py` → **0 match**. Nghĩa là candidate quan hệ do agent/member tạo **không có đường ghi xuống `canonical/entity_relations.jsonl`**.
- Mục tiêu task: **đóng vòng** entity → relation: candidate quan hệ đã review → ghi thành dòng trong `entity_relations.jsonl`, validate PASS.

## 1. Phạm vi (chỉ trong `AILAB_HANDOFF/app/backend`)

Mirror đúng pattern resolve/apply đang có cho entity/glossary/discourse/summary:
- `services/annotation_flow.py`: thêm nhánh xử lý `relation_candidates` (import → resolve → apply).
- `memory/store` (hoặc canonical writer hiện dùng cho các .jsonl): thêm hàm ghi/upsert `entity_relations.jsonl` theo `relation_id`.
- `routes/annotation.py`: endpoint resolve/apply relation (đi chung luồng với annotation hiện có; giữ human-review gate).
- Thêm unit test trong `app/backend/tests`.

**Không** tạo skill mới. **Không** đụng `ailab/dataset/` mirror (schema đã sync; đây là code chỉ ở handoff). **Không** đổi schema/validator.

## 2. Mapping candidate → dòng `entity_relations.jsonl`

Input (1 phần tử `relation_candidates`):
```
existing_relation_id, relation_key, source_ref, target_ref, relation_type,
suggested_address_policy{source_to_target/target_to_source:{self_term,address_term}},
state_label, valid_from_block_id, valid_to_block_id,
evidence[{block_id, surface, left_context, right_context}], reason, confidence
```
Output (1 dòng, đúng `entity_relation.schema.json`):
```
relation_id, doc_id, source_entity_id, target_entity_id, relation_type,
[state_label, valid_from_block_id, valid_to_block_id, trigger_event_id],
address_policy{source_to_target/target_to_source:{self_term,address_term}},
evidence[{block_id, surface}], confidence, notes
```
Quy tắc map:
- `source_ref` → `source_entity_id`, `target_ref` → `target_entity_id` (resolve `entity_key`/`existing_entity_id` → `entity_id`, dùng đúng resolver entity hiện có).
- `relation_key`/`existing_relation_id` → `relation_id` (nếu mới: sinh id ổn định, vd `rel_<src>_<tgt>_NN`, không trùng).
- `suggested_address_policy` → `address_policy` **chỉ sau khi người duyệt** (đây là draft).
- `evidence`: giữ `{block_id, surface}` (bỏ left/right_context khi ghi canonical — chúng chỉ để resolve).
- `relation_type`, `confidence`, `notes(reason→notes nếu muốn)`: pass-through.
- `trigger_event_id`: nhãn tự do optional (AI-LAB chưa có file events).

## 3. Quy tắc resolve (giữ nguyên kỷ luật AI-LAB)

- `source_ref`/`target_ref` **phải** resolve về `entity_id` tồn tại; nếu treo → **flag cho người chọn**, không tự bịa entity.
- `valid_from_block_id`/`valid_to_block_id` (nếu có) phải trỏ block tồn tại.
- **Relations KHÔNG có span** → không assert `clean_text[span]`; evidence chỉ cần `block_id` + `surface`.
- **Raw AI không bao giờ gold:** relation apply ở trạng thái **candidate/review**; `address_policy` chỉ thành "chốt" khi người duyệt. (entity_relation schema không có field `status` → trạng thái review quản ở `working/review_state.json` như các loại khác, không nhét status vào file canonical.)
- Pha: nếu candidate có `state_label`/`valid_from/to`, ghi nguyên; **không tự merge/đè** quan hệ default. Validator sẽ cảnh báo overlap (đã có), không cần check lại trong apply.

## 4. Tiêu chí nghiệm thu

- Một relation candidate đã review → xuất hiện thành 1 dòng hợp lệ trong `canonical/entity_relations.jsonl`.
- `python tools/validate.py --dataset <project canonical>` → **PASS**, `relations >= 1`; referential + overlap check chạy đúng.
- Human-review gate còn nguyên: output agent ở candidate, người duyệt mới apply `address_policy`.
- **75 backend test cũ vẫn OK** + thêm test mới: (1) happy path apply 1 relation; (2) `source_ref` treo → flag/không ghi; (3) 2 pha cùng cặp chồng lấp → validate cảnh báo.
- Lệnh chạy test: từ `AILAB_HANDOFF/` → `python -m unittest discover app/backend/tests`.

## 5. Ràng buộc / không làm

- Không đổi schema 1.5.0, không đổi `validate.py` (đã đúng).
- Không stage `ailab_projects/` (gitignored) hay file raw/source khi commit.
- Soft notes (`motifs/tone/implicit_meaning/narrative_note`) **ngoài phạm vi task này** — để optional/tương lai (Skill 3 JIT), không nhồi vào luồng bắt buộc.
- Reference subset (`manual_reference_subset.jsonl`) **ngoài phạm vi** — đã có human workflow (`working/drafts.json` → promote), không phải skill.

## 6. Tham chiếu
- Contract candidate: `skills/dataset-annotation-drafter/references/ANNOTATION_CANDIDATE_CONTRACT.md` (mục `relation_candidates` + "Resolve/Apply Contract").
- Schema đích: `dataset_spec/schema/entity_relation.schema.json`.
- Guideline quan hệ: `guidelines/ANNOTATION_GUIDELINE.md` (mục "Entity relations").
- Architecture xưng hô động: `../SCHEMA_AGENT_FILL_POLICY.md` (mục "Xưng hô động").
