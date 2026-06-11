# TASK — AILAB Dataset Schema 1.5.0: sidecar `entity_relations`

**Loại:** thay đổi schema dataset AI-LAB (contract) — bump `1.4.0 → 1.5.0`.
**Trạng thái:** ✅ ĐÃ implement trong repo này ở **cả 2 mirror**, `validate.py` PASS + negative
test bắt đúng lỗi. Spec này để **CodeX review/đối chiếu** (hoặc tái lập nếu CodeX giữ bản riêng).
**Chưa commit** (chờ người quyết).

---

## 0. Mục tiêu & lý do

Thêm một sidecar optional mô tả **quan hệ có hướng giữa hai entity**, chủ yếu để giải
**xưng hô tiếng Việt** (cặp gọi nhau / tự xưng) — lỗi dịch EN→VI quan trọng mà
`pronoun_policy` per-entity không nắm được (vì xưng hô phụ thuộc **cặp + chiều**).
Hỗ trợ cả **diễn biến quan hệ** (thân→thù) qua field pha. Đây là field hiếm hoi vượt
bar "truly necessary" nên mới đổi schema.

Ranh giới: schema này dùng chung 2 nhánh — **AI-LAB** (người annotate, AI nháp + human
duyệt) và **thesis** (Relation Agent tự điền, autonomous). Quyết định/architecture nằm ở
`SCHEMA_AGENT_FILL_POLICY.md` (mục "Xưng hô động"). 5 schema gốc **giữ nguyên**.

## 1. Phạm vi file (mỗi thay đổi áp **cả 2 mirror**, byte-identical)

Mirror A: `research/agent-based-translation/ailab/dataset/`
Mirror B (repo lồng): `research/agent-based-translation/AILAB_HANDOFF/dataset_spec/`

| File | Thao tác |
|---|---|
| `schema/entity_relation.schema.json` | **mới** |
| `sample/gold_demo_01/entity_relations.jsonl` | **mới** |
| `schema/document.schema.json` | sửa: `schema_version` const `"1.4.0"`→`"1.5.0"` |
| `sample/gold_demo_01/document.json` | sửa: `"schema_version": "1.5.0"` |
| `CHANGELOG.md` | thêm mục `## 1.5.0` |
| `tools/validate.py` | thêm load + referential + overlap check |
| `../DATASET_DESIGN.md` (thesis-side, 1 bản) | thêm §5.3b |
| `../SCHEMA_AGENT_FILL_POLICY.md` (thesis-side) | đã cập nhật mục "Xưng hô động" |

## 2. `entity_relation.schema.json`

- JSON Schema draft 2020-12, `additionalProperties:false`.
- **required:** `relation_id, doc_id, source_entity_id, target_entity_id, relation_type`.
- **optional:** `state_label, valid_from_block_id, valid_to_block_id, trigger_event_id,
  address_policy, evidence, confidence, notes`.
- `relation_type`: **free-text + recommended values** (sibling/parent/child/spouse/friend/
  master/servant/mentor/stranger/rival/creator_creation/guardian…), KHÔNG enum cứng.
- `address_policy`: object `{ source_to_target, target_to_source }`, mỗi chiều là
  `{ self_term, address_term }` (string|null). 4 chuỗi, hai chiều **độc lập**.
- `evidence`: array `{ block_id (req), surface (opt) }`.
- **Quy ước:** `source_entity` LÀ `relation_type` của `target_entity`
  (vd `relation_type=parent` ⇒ source là cha/mẹ của target).
- `trigger_event_id`: nhãn tự do, **chưa có FK** (AI-LAB 1.5.0 không có file events).
- **Pha:** `valid_from_block_id`/`valid_to_block_id` vắng/null ⇒ áp **cả tài liệu**.
  So sánh range theo **document order (`order_index`), KHÔNG so chuỗi block_id.**

## 3. Sample (`gold_demo_01/entity_relations.jsonl`) — 1 dòng, quan hệ ổn định

```json
{"relation_id":"rel_001","doc_id":"gold_demo_01","source_entity_id":"e_002","target_entity_id":"e_001","relation_type":"elder_and_child","address_policy":{"source_to_target":{"self_term":"ta","address_term":"cháu"},"target_to_source":{"self_term":"cháu","address_term":"ông"}},"evidence":[{"block_id":"gold_demo_01_ch01_b004","surface":"Clockkeeper"},{"block_id":"gold_demo_01_ch01_b005","surface":"You are simply early."}],"confidence":0.9,"notes":"Stable elder/child relation; no phase fields."}
```
(`e_002`=Clockkeeper, `e_001`=Mira — entity có thật trong sample. Không bịa pha vì excerpt không có bước ngoặt.)

## 4. `validate.py` — các thay đổi

1. `from collections import defaultdict`.
2. `DATA_FILES["entity_relation"]="entity_relations.jsonl"`, `SCHEMA_FILES["entity_relation"]="entity_relation.schema.json"`.
3. Trong lúc duyệt document, dựng `block_order = {}` và `block_order.setdefault(bid, len(block_order))` (global order theo thứ tự duyệt).
4. `er = validate_jsonl("entity_relation")`; `relation_ids = collect_ids(er, "relation_id", …)`.
5. Referential (mỗi dòng `er`):
   - `source_entity_id`, `target_entity_id` ∈ `entity_ids`;
   - `valid_from_block_id`, `valid_to_block_id` (nếu non-null) ∈ `block_ids`;
   - `evidence[].block_id` ∈ `block_ids`.
6. Overlap **warning** theo cặp `(source,target)`:
   - relation không có phase marker (`state_label`, `valid_from_block_id`, `valid_to_block_id`,
     `trigger_event_id`) = default relation. Nhiều default relation cho cùng cặp → warning.
   - relation có phase marker = explicit phase. Chỉ so overlap giữa các explicit phase với nhau.
     Default relation + phase override là use case hợp lệ, không warning.
   - range `[lo,hi]` với `lo=block_order[vf] (mặc định 0)`, `hi=block_order[vt] (mặc định N)`;
     sort theo `lo`, nếu `lo2 <= hi1` → cảnh báo overlap (không phải error).
7. `counts["relations"]` + in ra dòng summary.

## 5. Version bump

- `document.schema.json`: `"schema_version": { "const": "1.5.0" }`.
- `gold_demo_01/document.json`: `"schema_version": "1.5.0"`.
- `CHANGELOG.md`: mục `## 1.5.0` (xem repo).
- ⚠️ **Hệ quả:** mọi dataset đang ở 1.4.0 phải re-stamp `schema_version` lên 1.5.0 mới
  pass (validator yêu cầu const). Sidecar mới là optional nên không phá dữ liệu cũ ngoài
  con số version.

## 6. Tiêu chí nghiệm thu (đã đạt trong repo)

- `python tools/validate.py --dataset sample/gold_demo_01` → **PASS**, `relations=1`, cả 2 mirror.
- Negative test (đã chạy):
  - `source_entity_id` lạ → **error** `source_entity_id not in entities`.
  - `evidence.block_id` lạ → **error** `evidence block_id not in document`.
  - 2 pha chồng lấp cùng cặp → **warning** `overlapping phase ranges`.
- 6 file mirror **byte-identical** (SHA256 khớp).

## 7. Dùng ở runtime (thesis — NGOÀI phạm vi task dataset này, để tham khảo)

Precedence khi dịch block thoại:
```
block discourse.pronoun_hints / local tone override
> active relation phase theo order_index của block
> default relation (không phase)
> entity.pronoun_policy
> style fallback
```
Tính cách nhân vật **không thêm field** — biểu diễn qua `pronoun_policy` +
`key_events/open_threads/emotional_tone/tone/narrative_note`; quyết "leo thang vs kìm nén"
do Narrative/Translator agent sinh `pronoun_hint` JIT; Critic kiểm lệch phải có evidence.

## 8. Quyết định sau review CodeX

1. Giữ const bump `1.5.0` đúng convention repo; không thêm backward-compatible enum.
2. Không thêm sample có pha vào `gold_demo_01`; giữ sample sạch một quan hệ ổn định, tránh bịa
   bước ngoặt cho excerpt nhỏ. Dynamic phase sẽ test bằng fixture/probe riêng.
3. Provenance agent: giữ runtime mirror riêng cho thesis; không nới enum AI-LAB bằng
   `agent_unverified`.
