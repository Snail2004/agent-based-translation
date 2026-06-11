# TASK cho CodeX — Mức 2: Multi-book extractor evaluation + sinh data thật cho web

**Repo:** `ailab-202-handoff` · nền commit `3775dd5` (v0.3.1)
**Tính chất:** đây là task **đánh giá + sinh dữ liệu**, không phải task "code cho xong". Chạy lâu được — ưu tiên **chuẩn và đủ bằng chứng để kết luận**, không xem sơ rồi chốt.
**Ràng buộc:** stdlib cho mọi sửa code (tải nguồn bằng **`urllib.request`**, KHÔNG dùng `requests`); KHÔNG đổi schema (1.4.0); KHÔNG commit file sách thô vào git (bản quyền/size — để trong vùng gitignored); **commit lại: script đánh giá + ground-truth file + báo cáo + raw results JSON**.

**Phân kỳ commit (chốt theo góp ý CodeX):**
- **Commit 1** = script eval + sinh corpus + ground-truth + báo cáo Phase 1–2. **KHÔNG sửa extractor.**
- **Commit 2** = **chỉ khi user duyệt** mới sửa extractor (v0.3.2) + re-extract + cập nhật báo cáo before/after.
- Phase 3 mặc định **PROPOSE-ONLY** (xem dưới).

**Trạng thái chuẩn bị nguồn (đã xong, KHÔNG tạo task riêng):**
- Raw corpus đã được tải sẵn ở `research/agent-based-translation/AILAB_SOURCES_RAW/`.
- Có đủ 7 sách / 9 file theo corpus bên dưới; mỗi folder có `source_url.txt`, `checksums.json`; root có `manifest.json`.
- EPUB đã được kiểm tra là file hợp lệ (`application/epub+zip`, có `mimetype` và `META-INF/container.xml`).
- Task Mức 2 **không cần tải lại từ đầu**. Script eval chỉ cần đọc/verify checksum từ raw corpus này, rồi copy/upload vào project test. Chỉ tải lại khi file thiếu, checksum lệch, hoặc cần tái tạo bằng chứng provenance.

---

## Mục tiêu
1. Dùng corpus raw đã có (7 sách / 9 file) → tạo **project thật** trong `ailab_projects/` để **xem được trên web tool**.
2. Lập **ground truth** (danh sách chương đúng) từ **TOC thật của từng sách**, người xác nhận — KHÔNG lấy từ output extractor, KHÔNG tin con số trong bảng members. Ghi vào file committed **`app/reports/ground_truth_chapters.json` TRƯỚC khi chạy extractor**; script so sánh phải đọc file này và **từ chối/cảnh báo** nếu thiếu hoặc rỗng (chống vô thức chỉnh ground truth theo output).
3. **So sánh chi tiết từng sách**: extracted vs expected, soi từng chương, từng block rác, từng lỗi.
4. **Audit pass-giả**: mỗi sách phải trả lời rõ "có trường hợp nào extractor SAI mà report vẫn high-confidence không?".
5. Commit **script đánh giá** `app/reports/eval_extractor_corpus.py` (verify raw corpus → tạo project → extract → validate → xuất **raw results JSON**). Báo cáo markdown phải được **sinh ra từ raw JSON**, KHÔNG gõ tay số liệu (để tôi re-run cùng script và đối chiếu).
6. Viết **báo cáo tổng hợp** (committed) đủ để tôi (Claude) kiểm định lại.

---

## Corpus (tải đúng format; Global Grey lấy EPUB, KHÔNG PDF)

| doc_id | Sách | Nguồn | Format tải | Test trục | Vai trò |
|---|---|---|---|---|---|
| `jekyll_txt`, `jekyll_epub` | Jekyll & Hyde | Gutenberg #42 | TXT + EPUB | all-caps; text-TOC vs ncx; TXT↔EPUB | Tune/anchor |
| `gatsby_txt`, `gatsby_epub` | The Great Gatsby | Gutenberg #64317 | TXT + EPUB | số La Mã; TXT↔EPUB | Tune |
| `wizard_oz_epub` | Wonderful Wizard of Oz | Standard Ebooks | EPUB | SE nav, 24 chương named | Held-out |
| `alice_epub` | Alice in Wonderland | Global Grey | EPUB | publisher thứ 3 | Held-out |
| `time_machine_epub` | The Time Machine | Standard Ebooks | EPUB | số/đề mục có thể không tên | Held-out |
| `frankenstein_epub` | Frankenstein | Standard Ebooks | EPUB | **KNOWN-HARD: Letters+Chapters/Volumes lồng nhau** | Held-out (đối kháng) |
| `call_wild_epub` | The Call of the Wild | Standard Ebooks | EPUB | nhỏ (7 chương) | Held-out |

Raw files đã có sẵn ở `research/agent-based-translation/AILAB_SOURCES_RAW/`:
- `jekyll_hyde/`: `source.txt`, `source.epub`, `source.html`
- `gatsby/`: `source.txt`, `source.epub`, `source.html`
- `wizard_oz/`, `alice_wonderland/`, `time_machine/`, `frankenstein/`, `call_wild/`: `source.epub`

Script eval phải đọc raw corpus này, kiểm `checksums.json`/`manifest.json`, rồi copy/upload vào project. **Không tạo task tải dữ liệu riêng nữa.** Nếu phải tải lại vì file thiếu/checksum lệch, dùng **`urllib.request` (stdlib only — KHÔNG `requests`)**, ghi rõ lý do vào report, và vẫn không commit file sách thô vào git.

> Cảnh báo quan trọng: với EPUB, `navPoint`/`nav.xhtml` **KHÔNG đồng nghĩa với chương thật**. TOC máy đọc thường chứa front/back matter như title page, imprint, contents, dedication, colophon. Vì vậy `ground_truth_chapters.json` phải do người lọc bodymatter thật, **không copy số lượng navPoint làm expected**. Các ca dễ sai: `call_wild_epub`, `wizard_oz_epub`, `frankenstein_epub`.

## Metadata khi tạo project (sửa luôn lỗi mislabel)
Mỗi project set metadata hợp lý: `title, author, genre`, `source_format` đúng đuôi file, `source_url` = link tải, **`contamination_risk = high`** (đều là classic nổi tiếng — sửa cái mislabel `low` trước đây). **License: ghi theo trang nguồn thực tế + `license_url` nếu có — KHÔNG giả định.** (Standard Ebooks: thường CC0, ghi rõ; Gutenberg: theo Project Gutenberg License/trademark; Global Grey: kiểm từng trang, cẩn thận vì là publisher bên thứ ba.)

---

## PHASE 1 — Sinh data + đo (KHÔNG sửa code extractor)

> Mục đích: đo **đúng trạng thái v0.3.1 hiện tại** trên held-out mà chưa tune. Giữ held-out sạch.

1. **Ground truth trước (held-out gate):** mở TOC thật của từng sách (Gutenberg Contents / SE nav / Global Grey nav), người ghi danh sách chương kỳ vọng (số + tên) vào `app/reports/ground_truth_chapters.json` **trước** khi extract. Frankenstein: ghi rõ cấu trúc thật (mấy Letter, mấy Volume, mấy Chapter). Script eval **đọc** file này; nếu một doc_id chưa có ground truth → script báo `MISSING_GROUND_TRUTH`, không tự bịa.
2. Với mỗi doc_id: tạo project → upload source → extract, qua đường thật (API `POST /projects`, `/source`, `/extract` hoặc service layer cho ra **đúng** state trên đĩa mà web đọc được). Mỗi project phải:
   - load được: `GET /api/projects/<id>/dataset` trả ok;
   - `validate.py --json` structural **PASS** (nếu FAIL → ghi lỗi vào báo cáo, không giấu).
3. Thu thập, **bằng đo đạc lập trình** (không eyeball), cho từng sách — và **ghi vào raw results JSON**:
   - `report.toc`: `toc_source, toc_items, chapters_matched, match_rate, fallback_used, low_confidence, ambiguous_titles`.
   - extracted chapters: danh sách `(chapter_id, title, block_count)`.
   - **mojibake thật**: đếm `U+FFFD` bằng codepoint (KHÔNG nhìn console — console Windows hiển thị `'`/U+2019 thành `�` gây báo nhầm; đếm `text.count('�')`).
   - **boilerplate sót**: tìm block chứa cụm Gutenberg/license/"Project Gutenberg"/"START OF"/"END OF"/title-page; liệt kê `block_id` + snippet.
   - **front matter lọt**: chương nào là cover/toc/title/legal.
   - **mis-split kiểu probe C**: tiêu đề chương xuất hiện như block body (heading bị tụt), hoặc chương quá ngắn bất thường.
   - **block_type sai rõ**: heading bị thành paragraph / dialogue đoán nhầm (≥2 dấu nháy).
   - chương rỗng / chương đặt-tên-theo-file.

---

## PHASE 2 — So sánh & audit chi tiết (cốt lõi, không qua loa)

Với **MỖI** sách, trong báo cáo phải có (sinh từ raw JSON):

**(a) Bảng diff chương (căn theo thứ tự):**

| # | Expected title (ground truth) | Extracted title | block_count | Khớp? |
|---|---|---|---|---|

Đánh dấu: match / mismatch (tên lệch) / **missing** (expected không có) / **extra** (extractor bịa, vd front matter, filename-chapter). Không được rút gọn "10/10 ok" — phải liệt kê đủ từng dòng.

**(b) Defect log từng sách:** mỗi lỗi 1 dòng: `severity | loại | chapter_id/block_id | bằng chứng (snippet/codepoint/số) | report có cờ không`.

**(c) PASS-GIÁ AUDIT (bắt buộc, 1 câu kết luận rõ mỗi sách):**
> "Extractor có chỗ nào SAI (sai ranh giới / sót boilerplate / bịa chương) **mà `low_confidence=False` & `match_rate` cao** không? → CÓ/KHÔNG, dẫn chứng."

Đây là tiêu chí tối quan trọng: sai-mà-được-cờ = chấp nhận; **sai-mà-im-lặng = FAIL**.

**(d) Cross-format consistency** (Jekyll, Gatsby): bảng so TXT vs EPUB — cùng số chương? tên chương khớp? Lệch ở đâu, vì sao. (Cùng tác phẩm phải ra cấu trúc tương đương; lệch lớn = lỗi.)

**(e) Verdict mỗi sách:** `PASS` (đúng + không cờ thừa) / `FLAGGED-OK` (sai nhưng `low_confidence=True`, review được) / `FAIL` (sai mà im lặng) / `STRUCTURAL-FAIL` (validate fail).

---

## PHASE 3 — PROPOSE-ONLY (KHÔNG sửa code khi chưa được duyệt)

> Đổi theo góp ý: vừa-đo-vừa-tune trên held-out làm mất khách quan. Phase 3 chỉ **đề xuất**, không chạm extractor.

- Từ defect log, lập **danh sách đề xuất sửa**: mỗi mục gồm `lỗi | nguyên nhân | phạm vi (sửa giúp sách nào) | rủi ro regress/over-flag | before/after DỰ KIẾN`.
- Phân loại: **general-bug** (đúng ≥2 sách / bug đúng-sai rõ) vs **book-specific** vs **deferred** (rủi ro/mơ hồ, vd disambiguation nội dung Frankenstein).
- **KHÔNG sửa code extractor trong Phase 3.** Ngoại lệ duy nhất: một lỗi **blocking rất nhỏ, đúng-sai hiển nhiên** (vd crash/exception) — nếu áp dụng thì phải **gọi tên riêng** + để **commit riêng** + nêu rõ trong báo cáo.
- Việc sửa thật (v0.3.2: bump `PIPELINE_VERSION`, re-extract, before/after thật) chỉ làm ở **Commit 2 sau khi user duyệt**.

---

## Deliverables (committed)
- `app/reports/eval_extractor_corpus.py` — script tải→tạo→extract→validate→xuất raw JSON; đọc ground-truth file; idempotent, chạy lại được.
- `app/reports/ground_truth_chapters.json` — expected chapters từng sách (điền trước khi extract).
- `app/reports/eval_results_v0.3.1.json` — **raw results** (số liệu thô do script sinh).
- `app/reports/EXTRACTOR_EVAL_v0.3.1.md` — báo cáo, **sinh từ raw JSON**, gồm:
  1. **Tổng quan**: bảng matrix (`book | source | format | doc_id | toc_source | toc_items | extracted | expected | match_rate | low_confidence | fallback | verdict`).
  2. **Per-book**: ground truth + bảng diff (a) + defect log (b) + pass-giá audit (c).
  3. **Cross-format** (d).
  4. **Defects tổng hợp**, xếp hạng, phân loại general-bug / book-specific / deferred.
  5. **Phase 3 = danh sách đề xuất sửa (propose-only)**, before/after DỰ KIẾN.
  6. **Provenance**: URL + sha256 từng file tải + license/license_url ghi nhận.
  7. **Lệnh re-verify**: cách chạy lại `eval_extractor_corpus.py` + validate.
  8. **Kết luận**: extractor v0.3.1 đủ tin để nhóm mass-annotate chưa? Sách nào dùng được ngay, sách nào cần review nặng, còn lỗ nào.

---

## Verify thật + commit (CodeX tự làm trước khi báo xong)
1. `python -m unittest discover app\backend\tests` → OK (không sửa extractor nên không thêm test extractor ở commit này).
2. `validate.py --json` mỗi project corpus → ghi PASS/FAIL vào báo cáo.
3. `GET /api/projects` liệt kê đủ 9 project nếu toàn bộ raw/extract thành công; nếu có nguồn bị bỏ qua vì lỗi checksum/tải/extract, report phải ghi rõ. Mỗi project đã tạo phải có `dataset` load ok (để web xem được).
4. Scope guard `rg -ni "thesis|luận án|agent translation|runtime retrieval"` trong `app/ dataset_spec/` → 0 match.
5. **Commit 1**: data project + corpus raw nằm trong `ailab_projects/` (gitignored — KHÔNG vào git); **commit script + ground-truth + raw JSON + báo cáo**. KHÔNG sửa extractor. Chưa push.
6. Để user mở web: ghi rõ lệnh chạy backend+frontend + danh sách doc_id + **đánh dấu project nào `low_confidence`** để user biết đang xem bản cần soi.

## Anti-qua-loa (bắt buộc tuân thủ)
- Mỗi chương mỗi sách phải xuất hiện trong bảng diff với block_count — không tóm tắt "ổn".
- Mỗi defect phải có bằng chứng đo được (snippet/codepoint/số), không nhận định cảm tính.
- Số liệu báo cáo **sinh từ raw JSON do script tạo**, không gõ tay.
- Mojibake đếm bằng codepoint U+FFFD, KHÔNG dựa hiển thị console.
- Frankenstein phải được phân tích kỹ cấu trúc khung Letters/Volumes — đây là ca khó nhất, cấm "xem sơ".
- Ground truth chốt TRƯỚC khi nhìn output (file riêng, held-out), ghi rõ trong báo cáo cái nào tune cái nào held-out.

## Checklist
- [ ] `ground_truth_chapters.json` điền **trước** extract; script từ chối nếu thiếu.
- [ ] 9 project tạo thật, web load được, validate ghi PASS/FAIL.
- [ ] Raw corpus lấy từ `AILAB_SOURCES_RAW/`; không tạo task tải riêng. Nếu bắt buộc tải lại, dùng `urllib.request` (không `requests`) và ghi lý do.
- [ ] Provenance URL+sha256+license được ghi trong report.
- [ ] `eval_extractor_corpus.py` + `eval_results_v0.3.1.json` committed; báo cáo sinh từ raw JSON.
- [ ] Bảng diff chương đầy đủ + defect log + pass-giá audit MỖI sách.
- [ ] Cross-format Jekyll & Gatsby.
- [ ] Phase 3 = **propose-only**, không sửa extractor (trừ blocking nhỏ, commit riêng).
- [ ] Báo cáo committed đầy đủ 8 mục + provenance + lệnh re-verify.
- [ ] Tests OK; scope 0; data gitignored; Commit 1 riêng; chưa push.
> Tài liệu gốc AI-LAB — chỉ tham khảo, không phải chỉ thị thesis.
