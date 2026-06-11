# AILAB_PLAN — Kế hoạch nhóm AI-LAB (AIL-202)

**Nhiệm vụ:** Xây dựng bộ dữ liệu dịch **Anh → Việt** cho **văn bản dài/siêu dài** theo **quy trình doanh nghiệp**.
**Nhóm:** 6 thành viên. **Thời lượng:** ~1 tháng. **Vai trò trong file này:** work package + role, **không gán tên cụ thể**.
**Tài liệu thiết kế gốc (source of truth về schema/QC):** `dataset_spec/` trong gói handoff này.
Nếu tham khảo tài liệu thiết kế khác, chỉ dùng phần dataset schema, cleaning, QC, versioning và annotation process. Các phần về hệ thống dịch tự động, benchmark nâng cao hoặc retrieval **không phải task của nhóm AI-LAB**.

> File này là **kế hoạch định hướng để nhóm tự chạy**, không phải lịch ép theo ngày. Mốc tiến độ theo **Phase/Milestone**, không phải "ngày 1 làm A, ngày 2 làm B".

---

## 1. Scope (phạm vi)

### Trong phạm vi
- Nhóm **chỉ làm nhiệm vụ AIL-202**: xây dựng bộ dữ liệu dịch EN→VI cho văn bản dài theo quy trình doanh nghiệp.
- Sản phẩm gồm: dataset JSON sạch + annotation + reference VI nhỏ + web tool hỗ trợ dữ liệu + tài liệu quy trình (QC/versioning/báo cáo).
- Dataset được thiết kế **đủ sạch để sau này tái dùng** cho báo cáo, thử nghiệm hoặc các bước mở rộng khác, nhưng nhiệm vụ của nhóm vẫn chỉ là dataset và quy trình tạo dataset.

### Ngoài phạm vi (KHÔNG làm trong 1 tháng)
- **Không** xây hệ thống dịch tự động, multi-agent hoặc evaluation suite nâng cao.
- **Không** làm app dịch hay bất kỳ hệ thống sinh bản dịch nào.
- **Không** dịch parallel EN→VI cho toàn bộ văn bản.
- Web tool **chỉ** phục vụ thu thập/chuẩn hóa/annotation/QC/validation/progress — **không** phải app dịch.

> Các ý tưởng về hệ thống dịch tự động chỉ là bối cảnh định hướng, không đưa vào task của nhóm.

---

## 2. Triết lý dataset

- **Không chốt cứng** dataset là một tác phẩm duy nhất; **không chốt cứng** số nguồn / số tác phẩm / số block ngay từ đầu.
- Dataset **có thể gồm nhiều tác phẩm/nguồn** nếu nhóm làm được.
- **Làm được bao nhiêu thì làm**, nhưng **mỗi nguồn/block đưa vào bản freeze phải đạt chuẩn** (schema validate + provenance + QC pass).
- **Chất lượng, provenance, schema validation, annotation/QC, versioning > số lượng.**
- **MVP tối thiểu = một tập nhỏ nhưng hoàn chỉnh end-to-end** (chạy thông từ nguồn → JSON sạch → annotation → QC → version).
- **Target mở rộng = thêm nguồn/tác phẩm/block** sau khi pipeline đã ổn.
- Mọi con số dưới đây (nếu có) chỉ là **gợi ý/stretch range**, **không phải cam kết cứng hay deadline bắt buộc**.

Trong plan này, **nguồn MVP** nghĩa là nguồn đầu tiên đi hết vòng end-to-end và đạt QC: chọn nguồn → verify provenance/license → extract/clean JSON → validate → annotation cơ bản → review/QC → version. Các nguồn bổ sung chỉ được đưa vào sau khi nguồn MVP đã chứng minh pipeline chạy ổn.

---

## 3. Hình dạng dataset (định hướng MVP)

- **EN source sạch + annotation + reference VI capacity-based**: có mức tối thiểu để chứng minh quy trình, nhưng dịch/verify được càng nhiều đoạn càng tốt. Không bắt buộc parallel VI toàn bộ.
- **Provenance/license cho từng nguồn** (bắt buộc, không có ngoại lệ trong bản freeze).
- **Cleaning + QC + versioning** đầy đủ.
- **Python pipeline** cho extraction / cleaning / validation.
- **HTML/CSS/JS web tool** cho browser / annotation / review / QC / validation / progress.
- Đơn vị trung tâm: `block` (có `doc_id`, `chapter_id`, `block_id`, `source_text`, `clean_text`, annotation, provenance) — theo `../DATASET_DESIGN.md`.
- Tóm tắt chương lưu ở sidecar `chapter_summaries.jsonl`: AI-LAB chỉ cần `summary_source` + `source` cho nguồn MVP; các field narrative sâu là optional.

*Gợi ý quy mô (không cam kết cứng):* MVP có thể chỉ là 1 nguồn / một vài chương / vài chục block hoàn chỉnh; reference VI có mức tối thiểu để minh hoạ quy trình, sau đó mở rộng theo năng lực nhóm. Ưu tiên dịch/verify thêm nhiều đoạn trong cùng nguồn trước khi mở rộng sang nguồn mới nếu mục tiêu là có bản dịch tham chiếu dày hơn.

---

## 3.1. Ranh giới vai trò trong quy trình dữ liệu

| Vai trò | Làm gì | Không làm gì |
|---|---|---|
| **Schema** | Quy định file/field/id/span hợp lệ và dữ liệu nào được phép lưu. | Không tự đọc hiểu nội dung chương/đoạn. |
| **Pipeline** | Trích xuất, chia chapter/block, clean text, tạo JSON và provenance. | Không tự quyết định annotation văn học cuối cùng. |
| **Agent/AI assistant** | Có thể đề xuất term/entity, summary, tone, motif, narrative note. | Không ghi thẳng vào bản freeze nếu chưa được người duyệt. |
| **Annotator** | Điền/sửa dữ liệu theo guideline: term/entity, discourse, chapter summary, reference subset. | Không tự thêm field ngoài schema. |
| **Reviewer** | Xác nhận nội dung annotation; chốt dữ liệu đủ tin cậy. | Không bỏ qua validator/QC gate. |
| **Validator** | Kiểm schema, id, span, cross-reference và file liên kết. | Không đánh giá tóm tắt/ẩn ý/tone có đúng văn học hay không. |

Quy tắc freeze: dữ liệu do AI hỗ trợ chỉ được đưa vào bản freeze khi đã có trạng thái đã duyệt, ví dụ `source: "ai_assisted_verified"` trong `chapter_summaries.jsonl`, hoặc `status` đã duyệt ở glossary/entity/reference (`reviewed|locked`, `locked|human_verified` tùy file). Không đưa AI draft chưa duyệt vào dataset chính thức.

---

## 3.2. Field ownership: ai làm field nào?

Việc dùng AI assistant hoặc tool hỗ trợ KHÔNG có nghĩa là nhóm AI-LAB sẽ để AI tự hoàn thiện schema. Dataset được hoàn thiện bằng workflow nhiều executor:

| Nhóm field | Executor chính | Nguyên tắc |
|---|---|---|
| Structure/id/order/source/provenance | Code/pipeline | Tự động, deterministic, read-only sau khi sinh |
| `span`, `sentences[]`, term/entity occurrence offsets | Code hoặc UI selection | Không giao LLM tính offset |
| `clean_text`, `block_type`, `quality_flags` | Code draft + annotator review | Human sửa khi detect sai |
| Glossary/entity candidates, aliases, chapter summary | AI draft + annotator/reviewer verify | Không đưa AI draft chưa duyệt vào freeze |
| `motifs`, `tone`, `implicit_meaning`, `narrative_note` | Optional hint | Chỉ điền khi có giá trị; không bắt buộc mọi block |
| `reference_vi`, license, contamination risk, freeze decision | Human/reviewer/lead | Human-owned, review chéo, sign-off trước freeze |

Validator chỉ đảm bảo đúng schema và cross-reference. Chất lượng nội dung vẫn phải được reviewer xác nhận.

## 4. Work Packages (2 nhánh, không gán tên)

### Nhánh A — Data / Language
| WP | Mục đích | Output | Reviewer/Approver | Phụ thuộc | Phase |
|---|---|---|---|---|---|
| **A1** Chọn nguồn & license | Shortlist nguồn EN văn bản dài (public domain/CC); verify provenance/license; đăng ký raw + sha256 | `LICENSE_NOTES.md`, raw source đã đăng ký | Lead/Coordinator | — | P1 |
| **A2** Guideline & annotation spec | Guideline term/entity; **motif seed list (gợi ý 5–10)**; tone closed-set; MQM-lite; chốt field annotation | `annotation_guidelines/` | Lead + Annotation reviewer | A1 | P2 |
| **A3** Annotation + IAA + reference | Annotate term/entity + narrative cơ bản trên clean JSON; viết `summary_source` ngay sau khi review xong mỗi chương; overlap để tính kappa; **dịch/verify reference VI theo năng lực, càng nhiều càng tốt** | `glossary.jsonl`, `entities.jsonl`, `chapter_summaries.jsonl`, narrative annotation, `manual_reference_subset.jsonl`, IAA report | Annotation reviewer + Lead | A2, B2 | P3 |
| **A4** *(stretch)* QA/annotation extension | Bổ sung nhãn dữ liệu hoặc case QA phục vụ dataset reuse, ví dụ thêm motif/tone evidence, thêm reference subset, hoặc thêm nguồn nhỏ đã QC | Annotation/QC extension validate pass | Lead/Coordinator | A3 | P4 |

### Nhánh B — Web / Tooling
| WP | Mục đích | Output | Reviewer/Approver | Phụ thuộc | Phase |
|---|---|---|---|---|---|
| **B1** JSON Schema + validator | Viết `schema/*.json`; script validate (Python); thiết lập versioning/CHANGELOG | `schema/*.json`, validator chạy được | Lead/Coordinator | — | P1 |
| **B2** Extraction & cleaning pipeline | Python: EPUB/TXT/PDF → JSON theo schema; cleaning/normalization; quality flags | `document.json` validate pass | B1 owner + Lead | A1, B1 | P2 |
| **B3** Web tool | HTML/CSS/JS: dataset browser; annotation/review UI; validation/QC view; progress dashboard *(stretch)* | Web tool dùng được | Lead + B1/B2 reviewer | B1, B2 | P2→P3 |

### Definition of Done theo Work Package

| WP | Done khi |
|---|---|
| **A1** | Có ít nhất một nguồn MVP đã verify provenance/license, raw file được lưu/đăng ký, có sha256 và ghi rõ trong `LICENSE_NOTES.md`. |
| **A2** | `annotation_guidelines/` mô tả rõ term/entity, motif seed list, tone closed-set, quy tắc evidence và ví dụ đúng/sai; motif seed do Lead/Annotation Lead chốt. |
| **A3** | Annotation validate pass, có `summary_source` + `source` cho chương thuộc nguồn MVP, có overlap để tính IAA, có IAA report, reference VI đã dịch tới đâu thì phải được người song ngữ sửa và có review chéo tới đó. |
| **A4** | Chỉ làm nếu core đã xong; mọi extension vẫn phải validate pass, có provenance/QC, và không kéo nhóm sang hệ thống dịch/agent. |
| **B1** | Schema + validator chạy được trên golden sample; versioning/CHANGELOG có mẫu; lỗi validate hiển thị đủ để người làm dữ liệu sửa. |
| **B2** | `document.json` của nguồn MVP validate pass, block_id ổn định, không có block rỗng trong freeze, quality flags rõ. |
| **B3** | Web tool load được JSON, duyệt theo chapter/block, hỗ trợ review/annotation, validate/QC view hoạt động, export hoặc lưu JSON theo phương án đã chọn. |

### Xuyên suốt — Lead (điều phối)
Sở hữu schema, gác QC-gate, freeze version, làm việc với giảng viên hướng dẫn, ráp báo cáo, dựng Git + bảng tiến độ. Lead **không** là một WP riêng mà là vai điều phối toàn cục.

---

## 5. Timeline theo Phase (mốc tiến độ, không ép ngày)

> Nguyên tắc dependency cốt lõi: **chốt nguồn + schema/pipeline pilot phải đi TRƯỚC annotation full**; **schema phải đi TRƯỚC web annotation tool**; **clean JSON phải đi TRƯỚC annotation**.

### Phase 1 — Nền tảng & Spec (Foundation)
- **Mục tiêu:** dựng quy trình + chuẩn format trước khi sản xuất.
- **Input:** đề bài AIL-202, `../DATASET_DESIGN.md`.
- **Việc chính:** A1 (chọn nguồn + license), B1 (schema + validator), thiết lập Git/bảng tiến độ/versioning; pilot extraction nhỏ để có bản mẫu end-to-end.
- **Thứ tự nội bộ:** B1 schema/validator v0 phải có trước khi pilot được tính là "validate pass".
- **Output:** schema v1 + validator chạy; ≥1 nguồn có provenance/license; pilot JSON validate pass.
- **Dependency:** không (giai đoạn mở đầu).
- **Exit criteria:** ❑ schema freeze v1 ❑ validator pass trên pilot ❑ repo + bảng tiến độ + versioning sẵn sàng. **Không sang Phase 2 nếu schema chưa freeze v1 hoặc pilot chưa validate pass.**

Pilot của Phase 1 chính là **golden sample/spec kit** của nhóm: một tập 10-20 block đã validate pass, kèm README giải thích field quan trọng. Không tạo thêm một sample khác song song.

### Phase 2 — Pipeline & Khung công cụ
- **Mục tiêu:** pipeline extraction/clean ổn định + web tool đọc được dataset + guideline annotation sẵn sàng.
- **Input:** schema v1, pilot, nguồn đã chốt.
- **Việc chính:** B2 (extraction/clean full cho nguồn đã chốt → `document.json` validate pass), A2 (guideline + motif seed + tone set), B3 (browser đọc JSON).
- **Output:** clean JSON hợp lệ cho nguồn MVP; guideline chốt; tool hiển thị dataset.
- **Dependency:** Phase 1 (schema + pilot).
- **Exit criteria:** ❑ clean JSON validate pass cho nguồn MVP ❑ guideline annotation chốt ❑ tool browse được dataset. **Annotation full chỉ bắt đầu khi clean JSON + guideline đã xong.**

### Phase 3 — Annotation & QC (Production)
- **Mục tiêu:** sản xuất annotation + reference; tool ghi/sửa annotation; QC vòng 1; IAA.
- **Input:** clean JSON, guideline, tool browser.
- **Việc chính:** A3 (annotate term/entity + narrative + overlap IAA + reference VI capacity-based), B3 (annotation/review UI ghi ngược JSON + validation view), QC checklist vòng 1.
- **Output:** annotation files validate pass; IAA report; tool review được.
- **Dependency:** Phase 2.
- **Exit criteria:** ❑ annotation đạt DoD + validate pass ❑ IAA report có số ❑ QC vòng 1 pass.

### Phase 4 — Freeze, Mở rộng & Báo cáo
- **Mục tiêu:** freeze version, mở rộng nếu pipeline ổn, ráp báo cáo quy trình.
- **Input:** output Phase 3.
- **Việc chính:** QC toàn bộ → **freeze v1.0 + CHANGELOG**; *(stretch)* thêm nguồn, dịch/verify thêm reference VI, thêm annotation/QC extension hoặc dashboard nếu còn thời gian; viết báo cáo quy trình doanh nghiệp; chuẩn bị demo.
- **Output:** dataset freeze v1.0 + báo cáo + tool demo.
- **Dependency:** Phase 3.
- **Exit criteria:** ❑ toàn bộ QC checklist pass ❑ version freeze + CHANGELOG ❑ báo cáo quy trình hoàn chỉnh.

> Mở rộng (thêm nguồn/tác phẩm/block) chỉ làm **sau khi** một nguồn đã đi hết end-to-end và pipeline ổn — mỗi nguồn mới phải lặp lại P2→P3 ở quy mô nhỏ và đạt QC trước khi vào freeze.

---

## 6. Deliverables

1. **Dataset JSON sạch** (theo schema, có provenance/license từng nguồn).
2. **Annotation files** (glossary/entities + chapter summaries + narrative).
3. **Reference VI capacity-based**: có mức tối thiểu đã review, sau đó dịch/verify được càng nhiều càng tốt.
4. **Web tool** hỗ trợ dataset (browser/annotation/review/QC/validation/progress).
5. **Dataset README / data dictionary** giải thích từng file, field quan trọng, ID convention và cách dùng dataset.
6. **Annotation guidelines** cho term/entity, motif/tone, discourse, reference subset và review.
7. **Pipeline code + hướng dẫn chạy** cho extraction/cleaning/validation.
8. **IAA report** cho phần annotation overlap.
9. **QC checklist** (đã chạy, có kết quả).
10. **Versioning/CHANGELOG** (semver + ghi log thay đổi).
11. **Báo cáo quy trình doanh nghiệp** (pipeline, schema, provenance, cleaning, annotation, QC, versioning).

---

## 7. Definition of Done (kiểm tra được)

### Dataset / JSON
- [ ] Mọi block có `doc_id`/`chapter_id`/`block_id`/`order_index`/`clean_text` (không null/empty).
- [ ] 100% file `document.json` **validate pass** qua `schema/*.json`.
- [ ] Mỗi nguồn trong freeze có `LICENSE_NOTES` (license + URL + ngày) và `raw_sha256`.
- [ ] Không còn page number/header/footer trong `clean_text` (kiểm mẫu ≥20 block).
- [ ] Block lỗi extraction được flag và loại khỏi bản freeze nếu không sửa được.

### Annotation
- [ ] Term/entity có `expected_target`/variants/`span`/`block_id`; mention map về `entity_id`.
- [ ] Narrative annotation chỉ dùng **motif seed list**; motif mới đánh dấu suggestion.
- [ ] `chapter_summaries.jsonl` có `summary_source` + `source` cho chương thuộc nguồn MVP; viết ngay sau khi chapter annotation/review xong, không dồn cuối dự án.
- [ ] `implicit_meaning` chỉ ghi khi có evidence, còn lại `null`.
- [ ] Có **IAA report** (kappa) cho phần overlap; ghi rõ số và cách xử lý nếu thấp.

### Reference VI
- [ ] Có tối thiểu một tập stratified (chapter-opening/dialogue/narrative/term-heavy/random) đã dịch + review chéo để chứng minh quy trình.
- [ ] Sau mức tối thiểu, dịch/verify được tới đâu đưa vào `manual_reference_subset.jsonl` tới đó; càng nhiều càng tốt, miễn mọi dòng đều có `source` + `status` và được review.
- [ ] Không bắt buộc parallel VI toàn bộ. Nếu nhóm dịch được gần/toàn bộ nguồn thì vẫn hợp lệ, nhưng phải giữ nguyên quy tắc reviewed/locked và provenance AI nếu có.

### Web tool
- [ ] **Browser:** load JSON, hiển thị theo chapter/block, highlight entity/term.
- [ ] **Annotation/Review:** thêm/sửa annotation, ghi ngược JSON giữ ref-by-id.
- [ ] **Validation/QC view:** chạy validator, hiển thị block lỗi/flag.
- [ ] Import JSON → edit → export JSON hoạt động (hoặc local server nhỏ nếu nhóm làm được).
- [ ] *(stretch)* dashboard tiến độ (% clean/annotate, kappa).

### Quy trình
- [ ] Git repo + bảng tiến độ; mỗi WP có DoD.
- [ ] `versions.json` + `CHANGELOG.md`; **test/freeze set không sửa sau khi freeze** (đổi sau freeze → tăng version).
- [ ] Báo cáo quy trình có đủ: pipeline, schema, provenance, cleaning rules, QC checklist, versioning, IAA.

---

## 8. Cut-line (nếu thiếu thời gian, cắt theo thứ tự)

1. Bỏ **A4 QA/annotation extension**.
2. Bỏ **progress dashboard** của web tool.
3. **Giảm số nguồn/số block** (giữ ít nhưng hoàn chỉnh end-to-end).
4. **Dừng mở rộng reference VI** ở mức tối thiểu đã review.

**KHÔNG được cắt:** JSON **schema + validation**, **QC**, **versioning/CHANGELOG**, **dataset README/data dictionary**, **annotation guidelines**, **pipeline run guide**, **web tool core (browser + review)**. Đây là phần thể hiện "quy trình doanh nghiệp" và là phần lõi được chấm.

---

## 9. Risks & giảm thiểu

| Rủi ro | Hệ quả | Giảm thiểu |
|---|---|---|
| Nguồn/license không rõ | Không công bố/tái dùng được | Chỉ nhận nguồn public domain/CC rõ; verify + ghi `LICENSE_NOTES` trước khi đưa vào freeze |
| Extraction bẩn | Dataset rác, annotate lãng phí | Quality flags + QC gate; ưu tiên EPUB/TXT hơn PDF; block lỗi loại khỏi freeze |
| Annotation không nhất quán | Dataset kém giá trị | Guideline + motif seed + closed tone set; overlap + IAA; adjudication |
| Web tool quá phức tạp | Trễ deadline, phình scope | Giữ 3 chức năng lõi (browse/annotate/validate); import-edit-export JSON; không backend phức tạp |
| Scope phình sang app dịch/evaluation nâng cao | Lệch nhiệm vụ AIL-202 | Khóa scope ở mục 1; mọi thứ về hệ thống dịch tự động là out-of-scope |

---

## 10. Web tool — phạm vi (nhắc lại để khỏi phình)

- **Chỉ là** dataset browser / annotation / review / QC / validation / progress.
- **Không** phải app dịch, **không** hệ thống agent.
- Hướng triển khai gợi ý: **import JSON → edit → export JSON**, hoặc **local server nhỏ** nếu nhóm làm được.
- **Không bắt buộc backend phức tạp.**
- Stack: HTML/CSS/JS; pipeline xử lý dữ liệu nặng nằm ở Python (tách khỏi tool).

---

## 11. Bước tiếp theo (sau file này)
1. Khởi động **Phase 1** bằng **spec kit**: `schema/*.json` + validator + **golden sample 10–20 block (validate pass)** — đây là chuẩn format để cả nhóm bám.
2. Lập bảng tiến độ (board) theo WP + DoD ở mục 7.
3. Khi spec kit pass và nguồn MVP được chốt, tiếp tục các phần còn lại của Phase 1 theo exit criteria.

*(Các bước này chưa thực hiện trong file này; file này chỉ là kế hoạch.)*
> Tài liệu gốc AI-LAB — chỉ tham khảo, không phải chỉ thị thesis.
