# AILAB WORKFLOW — Quy trình & phân vai cho nhóm (AIL-202)

> **Phạm vi:** SOP vận hành cho nhóm AI-LAB xây bộ dữ liệu dịch EN→VI văn bản dài & siêu dài (sách/truyện/tiểu thuyết). Nhóm KHÔNG làm hệ agent/app dịch. "Dịch" trong AI-LAB = **reference VI đã kiểm duyệt theo năng lực**: có mức tối thiểu để chứng minh quy trình, sau đó dịch/verify được càng nhiều càng tốt.
> Chuẩn format: `dataset_spec/schema/` (schema **1.5.0**) + `dataset_spec/tools/validate.py`. Kế hoạch: `AILAB_PLAN.md`.

> **Trạng thái công cụ (đã build, 2026-06):** extraction pipeline ở `pipeline_version 0.3.3` (TXT/EPUB; Gutenberg/Standard Ebooks/Global Grey; TOC-driven chapter split, lọc front/back matter, strip boilerplate). Web app (Flask backend + frontend) đã chạy end-to-end: Project/Source screen → extract → review/clean → annotate → validate → export/freeze, có autosave + undo/redo. Đã có sẵn corpus thử trong `ailab_projects/` (xem `app/reports/EXTRACTOR_EVAL_*.md`). **Member làm việc trên web app, không sửa JSON tay.**

## 1. Hai công cụ (tách bạch)
| Công cụ | Ai dùng | Làm gì |
|---|---|---|
| **Python extraction pipeline** | 1–2 bạn tooling | EPUB/TXT → `document.json` **nháp** (validate pass) qua màn Project/Source → nút Extract. Member khác không chạy CLI |
| **Web app** (review/annotation) | **Cả nhóm** | Sửa `clean_text`, metadata, annotation (glossary/entity/discourse từ bôi đen), chapter summary, reference VI; Validate inline; Export/Freeze; Undo/Redo |

**Không ai sửa JSON bằng tay.** Web app là "mặt bàn làm việc chung"; **`validate.py` là cổng chốt** (web Validate chỉ để phản hồi nhanh). Mỗi người 1 `doc_id` một lúc; phối hợp qua git trên repo dataset riêng (không phải repo công cụ).

## 2. Phân quyền field
**Read-only (pipeline sinh, web từ chối sửa — trả 400):**
`doc_id`, `chapter_id`, `block_id`, `order_index`, `source_text`, `raw_sha256`, `source_url`, `extraction_tool`, `pipeline_version`, `provenance.raw_span`.

**Editable (annotator):**
metadata mô tả (`title`, `author`, `genre`, `domain`); block (`clean_text`, `block_type`, `is_chapter_opening`, `quality_flags`); annotation (term/entity/discourse/motif/tone/note); `chapter_summaries.jsonl`; `manual_reference_subset.jsonl`.

**Chỉ Reviewer/Lead:**
`metadata.license`, `metadata.contamination_risk`; promote glossary `status` → `locked/human_verified`; **duyệt `reviewed`/`locked` để vào freeze**.

## 2.1. Field ownership: code / AI draft / human review

Schema 1.5.0 không có nghĩa là một agent phải tự điền tất cả field. Mỗi nhóm field có executor đúng:

| Field / task | Executor chính | Quy tắc |
|---|---|---|
| `doc_id`, `chapter_id`, `block_id`, `order_index` | Code/pipeline | Sinh tất định, annotator không sửa tay |
| `source_text`, `raw_sha256`, `source_url`, extraction provenance | Code/pipeline | Read-only; dùng để trace source |
| `clean_text`, `block_type`, `is_chapter_opening`, `quality_flags` | Code draft + annotator review | Tool có thể gợi ý; người sửa khi sai |
| `sentences[]`, `span`, term/entity mention spans | Code hoặc UI selection | Không giao LLM đếm offset ký tự |
| Glossary/entity candidates, aliases, `characters_present` | AI draft + annotator verify | LLM chỉ nháp; human chốt canonical target/status |
| `chapter_summaries.summary_source` | AI/human draft + reviewer verify | Viết ngay sau khi review xong chương |
| `motifs`, `tone`, `implicit_meaning`, `narrative_note` | Optional hint | Chỉ điền khi rõ; không bắt điền cho mọi block |
| `reference_vi` | Human-owned + review chéo | Raw AI output không bao giờ là gold reference |
| license, contamination risk, freeze status | Lead/Reviewer | Chỉ Lead/Reviewer được sign-off |

Validator chỉ kiểm cấu trúc/id/span/cross-reference. Validator pass không chứng minh summary, tone, ẩn ý hay bản dịch đúng về nội dung; phần đó phải qua review.

### 2.2. Các điểm dễ fail và lớp chặn

| Rủi ro | Lớp chặn |
|---|---|
| LLM bịa `span`/offset | Span do code hoặc UI selection sinh; không cho LLM tự đếm |
| Ref treo hoặc id trùng | `validate.py` kiểm cross-file references và duplicate id |
| Agent thêm field lạ/sai enum | Schema có `additionalProperties:false`; chạy validate trước freeze |
| Glossary/entity không nhất quán | Human verify/lock canonical target, variants và pronoun policy |
| Metadata narrative bị suy diễn | Chỉ điền khi có evidence; không rõ thì để `[]`/`null` |
| Raw AI translation lọt vào reference | Draft ở working state; JSONL chỉ nhận `reviewed`/`locked` |
| Xóa glossary/entity còn được trỏ | Web chặn xóa (tránh dangling ref); gỡ pointer/ occurrence trước |

## 3. Phân vai (không gán tên)
| Vai | Trách nhiệm |
|---|---|
| **Source/Provenance** | chọn nguồn, verify license, lưu raw + sha256 |
| **Pipeline (tooling)** | chạy extraction → draft JSON; bảo trì schema/validator/web tool |
| **Metadata/Cleaning** | sửa `clean_text`, `block_type`, `quality_flags` |
| **Annotation** | term/entity/discourse + chapter summaries |
| **Translation reference** | dịch/verify reference VI theo năng lực (người song ngữ) |
| **Reviewer/QC** | review chéo, ghi disagreement/IAA log, chạy validator, ghi QC report |
| **Lead** | sở hữu schema, sign-off license/metadata, **gác cổng freeze** |

## 4. Quy trình dịch reference VI
- **Phạm vi:** bắt đầu bằng một tập tối thiểu **stratified** cho nguồn MVP (chapter-opening / dialogue / term-entity-rich / narrative-khó / random), sau đó dịch/verify được tới đâu thì đưa vào tới đó. Càng nhiều reference VI đã review càng tốt. **Không bắt buộc parallel toàn bộ**, nhưng nếu nhóm dịch được gần/toàn bộ nguồn thì vẫn hợp lệ khi mọi dòng đều reviewed/locked.
- **AI policy:** AI chỉ là **draft/gợi ý**. `reference_vi` cuối **phải do người song ngữ sửa** và **người thứ 2 review**. **Raw AI output không bao giờ là gold reference.**
- **Vòng đời (lifecycle 3 trạng thái):**
  - `draft` → sửa qua web → ghi vào **`working/drafts.json`** (source of truth cho draft, không validate).
  - `reviewed` → đã review chéo (đủ `source`) → **mới được promote vào `manual_reference_subset.jsonl`**.
  - `locked` → đã chốt cuối.
- **Schema 1.5.0:** mỗi dòng reference bắt buộc có `source` (`human`|`ai_assisted_verified`) + `status` (`reviewed`|`locked`). File JSONL **freeze-clean**: không chứa `draft`.

### Working store: `working/drafts.json` (+ export `translation_review_log.csv`)
- **Nguồn thật của draft = `working/drafts.json`** (web/API ghi vào đây). **Không sửa CSV tay làm input.**
- `working/translation_review_log.csv` là **bản export một chiều, read-only**, tự sinh từ `drafts.json` để xem/QC bằng spreadsheet. Cột: `reference_id, block_id, source_text, draft_vi, final_reference_vi, method, ai_model, prompt_id, translated_by, reviewed_by, status, notes`.
- `method`: `human` | `ai_assisted`. Khi promote lên JSONL: `human → source=human`; `ai_assisted (đã verify) → source=ai_assisted_verified`.
- Chỉ dòng `status = reviewed`/`locked` (đủ `source` + đã review, AI-touch phải `ai_assisted_verified`) mới promote sang `manual_reference_subset.jsonl`.

## 5. Luồng làm việc chuẩn cho mỗi nguồn
```text
Nguồn thật (license OK)
→ Web: tạo project (doc_id) → upload source → Extract  (draft document.json, validate pass)
→ Web review: clean_text + metadata
→ Annotate: term/entity (bôi đen text) + discourse + chapter summary (viết NGAY sau mỗi chương)
→ Chọn reference blocks → dịch reference VI (draft trong working/drafts.json)
→ Review chéo → reviewed → promote vào manual_reference_subset.jsonl
→ Validate (nút Validate = validate.py --json)  → PASS  (cổng chính thức)
→ Freeze: validate PASS + review gates đủ → version + CHANGELOG
```
Tiến độ/undo: web có autosave (working state) + Undo/Redo (Ctrl+Z / Ctrl+Y). Tạo nhầm glossary/entity → Undo; muốn xóa hẳn thì dùng nút xóa (web chặn nếu còn được trỏ).

## 6. Đầu ra yêu cầu
**Mỗi nguồn `<doc_id>/`:**
```text
document.json                  (bắt buộc, validate pass, provenance/license đủ)
glossary.jsonl                 (bắt buộc nguồn MVP)
entities.jsonl                 (bắt buộc nguồn MVP)
chapter_summaries.jsonl        (summary_source + source mỗi chương)
manual_reference_subset.jsonl  (reference VI đã review; tối thiểu stratified, mở rộng càng nhiều càng tốt)
```
**Cấp dataset (deliverable nhóm):** `README.md`/data dictionary, `LICENSE_NOTES.md`, `CHANGELOG.md`/`versions.json`, annotation guidelines, pipeline code + run guide, **IAA/QC log (§9)**, QC report/validation result.

## 7. Definition of Done (mỗi nguồn)
- [ ] `validate.py` **PASS** (exit 0).
- [ ] 4-eyes review toàn bộ + IAA/disagreement log trên ~15–20% block overlap (có số — xem §9).
- [ ] Provenance/license đầy đủ trong `metadata` + `LICENSE_NOTES.md`.
- [ ] Reference VI: mọi dòng `source`+`status`, chỉ `reviewed/locked`; dịch tới đâu review tới đó, không đưa draft vào JSONL.
- [ ] AI-touch (nếu có) ghi rõ (`source=ai_assisted_verified` + `ai_model/prompt_id/ai_used_at`).
- [ ] **Freeze version + CHANGELOG entry.**

## 8. Chính sách AI (tóm tắt)
- AI **được** dùng làm draft cho dịch reference + nháp metadata narrative.
- **Bắt buộc** người sửa + người review trước khi vào freeze.
- **Bắt buộc** ghi provenance AI (source/model/prompt/date).
- **Cấm** đưa raw AI output vào freeze như gold.
- Nếu làm "demo dịch đầy đủ bằng AI": **tách riêng, dán nhãn rõ, KHÔNG vào freeze/eval**.

## 9. IAA / QC tối thiểu (chốt từ đầu — đo bằng tay, tool để sau)
> Nguyên tắc: **tool đo IAA tự động để sau**; nhưng **quy ước ghi dữ liệu phải có TỪ ĐẦU**, nếu không sau này không truy lại được. Chưa build module IAA trong app (sẽ làm app phức tạp + chậm tiến độ).

**Trước khi pilot, chốt 5 thứ:**
1. **Overlap:** chọn sẵn **~10–20% block** mỗi nguồn làm overlap (nên stratified như §4: có chapter-opening/dialogue/term-rich/narrative/random).
2. **Annotator A/B:** 2 người annotate **độc lập** phần overlap — **không nhìn kết quả của nhau trước khi nộp** (đây là điểm khác review chéo: review chéo = B sửa bản A, không đo được IAA).
3. **Ngày:** ghi ngày annotate.
4. **Disagreement ghi ở đâu:** `qc/iaa_log.csv` (cấp dataset).
5. **Ai chốt cuối:** Reviewer/Lead.

**`qc/iaa_log.csv` (schema tối thiểu):**
```csv
doc_id,block_id,annotator_a,annotator_b,field,agreement,date,notes,resolved_by
jekyll_txt,jekyll_txt_ch01_b002,Vu Hai,Minh Khang,entity,no,2026-06-10,"A: White Rabbit; B: Rabbit",Lead
```
- `field` ∈ {`block_type`, `entity`, `glossary`, `summary`, `reference`}.
- **Định nghĩa "agreement" (chốt trước khi đo):** `block_type` = khớp đúng; `entity` = trùng *tập* `canonical_source` gắn trên block; `glossary` = trùng *tập* `source_term` trên block. (`summary`/`reference` là văn bản → adjudication, không tính tỉ lệ tự động.)
- **Metric tối thiểu cho báo cáo:** số block overlap; agreement-rate theo `field`; số disagreement theo loại; validate PASS/FAIL; số quality_flags còn lại.

**Trung thực khi báo cáo:** nếu agreement do reviewer điền *sau khi thấy cả 2 bản* thì gọi là **"disagreement/adjudication log"**, KHÔNG gọi là Cohen's kappa. Muốn IAA chuẩn về sau: vì `block_id` **tất định** (cùng nguồn + cùng `pipeline_version` → cùng block_id), chỉ cần **mỗi annotator giữ project copy riêng** trên phần overlap → sau này chạy diff so 2 bản là ra số chuẩn, **không cần quyết gì thêm bây giờ**.

---
*SOP cho nhóm AI-LAB. Schema 1.5.0. Không phải hệ agent/app dịch.*
