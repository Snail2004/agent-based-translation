# TASK_EV_01_consistency_metrics — Module TAR/ECS/FVR + đo lần đầu trên output oracle

- **Status:** READY
- **Refs:** THESIS_ARCHITECTURE_LOCK §6.2 (định nghĩa TAR/ECS, consistency≠verbatim), §6.3 (oracle: việc-ngay-khi-dịch-xong), PROMPT_DESIGN §1.6; dữ liệu oracle trong `AILAB_HANDOFF/ailab_projects/treasure_island/` (CHỈ ĐỌC)
- **Branch/Commit:** (điền khi imple xong; commit `EV-01: ...`)

## 1. Bối cảnh & mục tiêu

Oracle run đã dịch xong 40/40 chương (1.476 blocks). Đo TAR/ECS internal trên output
đó = (a) **con số metric đầu tiên của dự án** mang đi báo GVHD, (b) kiểm chứng định
nghĩa §6.2 vận hành được trước khi thành vũ khí chính, (c) tạo module `pipeline/eval/`
DÙNG LẠI NGUYÊN VẸN cho S0/S3 sau này — cùng một thước mới so được. 0 token LLM —
thuần Python.

## 2. Scope

**IN:**
1. `pipeline/eval/consistency.py` — hàm thuần, input tổng quát (không hardcode đường
   dẫn AI-LAB): registry (terms, entities) + occurrences/mentions per block +
   translations per block → scores.
2. `pipeline/eval/loaders.py` — adapter đọc dữ liệu oracle từ project AI-LAB
   (READ-ONLY): `glossary.jsonl`, `entities.jsonl`, `document.json` (annotations
   term_occurrences/entity_mentions per block), `working/translation_preview/agent_outputs/*_preview.json`
   (block_id → target_text).
3. `pipeline/scripts/score_consistency.py` — CLI:
   `--project <path>` `--out data/reports/oracle_consistency.json` → in bảng console
   + ghi report JSON. `data/reports/` ĐƯỢC track git (kết quả nhỏ, cần pin).
4. Tests offline với fixture tự tạo có giá trị tính tay được.

**OUT:** LLM-judge, COMET, backtranslation (pha sau); ghi vào bảng `evaluation_runs`
(oracle không nằm trong translation_runs của thesis DB — report file là đủ; S0/S3 sau
này mới ghi DB); KHÔNG sửa bất cứ gì trong AILAB_HANDOFF (read-only tuyệt đối).

## 3. Spec — định nghĩa metric (đây là phần quan trọng nhất, làm ĐÚNG từng chữ)

### 3.0. Chuẩn hóa text (dùng chung mọi matching)
- Unicode NFC + casefold. KHÔNG bỏ dấu tiếng Việt.
- Match theo word-boundary unicode: regex `(?<!\w)<term>(?!\w)`, term được
  `re.escape`, cho phép multi-word. Áp cho cả source (EN) lẫn target (VI).

### 3.1. TAR — Terminology Adherence Rate
- **Đơn vị = cặp (block_id, term_id)** (quyết định: không đếm per-occurrence làm chính
  vì target không align occurrence được khi gộp/tách câu — tránh phạt oan kiểu §1.6).
- Nguồn occurrence: `document.json` annotations `term_occurrences` (đã span-resolve,
  chính xác). Code nhận occurrences như INPUT — sau này S0/S3 sẽ có provider khác
  (string-match) cùng interface.
- Mỗi cặp (block, term):
  - term thường: **adherent** nếu target_text chứa `expected_target` HOẶC một
    `allowed_variants[]` (match theo §3.0).
  - term `do_not_translate=true`: adherent nếu target chứa NGUYÊN source_term.
- `TAR = adherent_pairs / total_pairs`. Báo cáo: tổng + per-chapter + per-term (để
  thấy term nào hay rớt). Cột phụ: occurrence-weighted TAR (cùng logic, trọng số =
  số occurrence trong block).

### 3.2. FVR — Forbidden Variant Rate
- Đếm mọi lần một `forbidden_variants[]` xuất hiện trong target_text của block có
  occurrence term đó. `FVR = blocks_vi_phạm / total_pairs` + danh sách vi phạm
  (block_id, term, variant) để soi tay.

### 3.3. ECS v1 — Entity Consistency Score (approved-form coverage)
- Nguồn mention: `document.json` annotations `entity_mentions`. **CHỈ tính mention
  dạng tên** (mention_type/name surface — nếu annotation không phân loại thì lọc:
  surface trùng canonical_source hoặc aliases_source; mention đại từ/lược → LOẠI khỏi
  mẫu số — đúng consistency≠verbatim).
- Approved forms của entity = `canonical_target` ∪ `aliases_target[]` (lọc rỗng).
- Per entity: `coverage = blocks_có_approved_form_trong_target / blocks_có_name_mention`.
- `ECS = trung bình coverage theo trọng số số name-mention-blocks`. Báo cáo: tổng +
  per-entity (top 15 theo mention count) + **bảng phân bố dạng tên đã dùng**
  (form → count) per entity để soi drift bằng mắt.
- **Limitation khai rõ trong docstring + report**: v1 không phát hiện "dạng sai chưa
  đăng ký" (vd phiên âm lạ) — bắt được khi approved form vắng mặt; refinement = V2.
- Entity không có `canonical_target` (registry chưa điền) → loại khỏi tính, đếm
  riêng `entities_skipped`.

### 3.4. Report JSON (schema output)
```json
{
  "project": "treasure_island", "scored_at": "...",
  "source": "oracle_gpt55_preview", "metric_version": "consistency_v1",
  "tar": {"overall": 0.0, "pairs": 0, "occurrence_weighted": 0.0,
           "per_chapter": {"ch01": 0.0}, "worst_terms": [{"term": "", "rate": 0.0, "pairs": 0}]},
  "fvr": {"overall": 0.0, "violations": [{"block_id": "", "term": "", "variant": ""}]},
  "ecs": {"overall": 0.0, "entities_scored": 0, "entities_skipped": 0,
           "per_entity": [{"entity": "", "coverage": 0.0, "name_mention_blocks": 0,
                            "forms_used": {"form": 0}}]},
  "inspection": {"lowest_tar_blocks": ["block_id x10"], "lowest_ecs_entities": ["..."]}
}
```

## 4. Acceptance criteria (lệnh chạy được)

```bash
cd research/agent-based-translation/THESIS_RUNTIME_TOOL

python -m pytest pipeline/tests/test_consistency.py -v
# PHẢI PASS (fixture tự tạo, giá trị TÍNH TAY ghi trong comment test):
# 1. test_tar_basic: 4 cặp, 3 adherent (1 dùng expected, 1 dùng allowed_variant,
#    1 do_not_translate giữ nguyên), 1 miss → TAR = 0.75
# 2. test_tar_variant_not_verbatim: target dùng allowed_variant, KHÔNG chứa
#    expected_target → vẫn adherent (chốt §1.6)
# 3. test_fvr_detects: target chứa forbidden variant → violation ghi đúng block/term
# 4. test_ecs_pronoun_excluded: mention đại từ không vào mẫu số; coverage tính đúng
#    theo name-mentions; alias_target được chấp nhận như canonical
# 5. test_ecs_skips_unfilled_entity: entity thiếu canonical_target → skipped count
# 6. test_word_boundary: term "rum" KHÔNG match "rumor"; match case-insensitive + NFC

# Chạy thật trên oracle (read-only AILAB_HANDOFF):
python -m pipeline.scripts.score_consistency --project "../AILAB_HANDOFF/ailab_projects/treasure_island" --out data/reports/oracle_consistency.json
# - exit 0; report ghi ra; console in: TAR overall, FVR, ECS overall,
#   top-5 worst terms, top-5 entities theo coverage thấp
# - sanity: pairs > 0, 0.0 <= mọi score <= 1.0, đủ 40 chapter keys trong per_chapter

python -m pytest pipeline/tests/ -v   # toàn bộ pipeline tests vẫn PASS
```

## 5. Implementation notes *(CodeX điền)*

—

## 6. Review *(Claude điền)*

—
