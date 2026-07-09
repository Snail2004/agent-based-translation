# Q7 novel reference survey: English public-domain novel with complete Vietnamese published translation

## Final decision update (2026-07-08)

**Selected corpus:** `Wuthering Heights / Doi gio hu`.

**Reason:** this is the best available balance between memory/xung-ho difficulty and practical run size, and the user supplied a legal Vietnamese EPUB candidate that passes the full-structure gate:

- Local VI file: `reference/literary/wuthering_heights/vi/doi_gio_hu_vi_full_34ch_candidate.epub`
- VI SHA256: `6737B7DC333C7795A5BB6987274C78C27425DA8B842C89295AA62C2B5B4B84BE`
- TOC: 34 numbered chapters + introduction + appendix
- Chapter-1 spot check: no abridgement signal against Project Gutenberg EN chapter 1
- Caveat: translator/edition metadata is not exposed in the EPUB, so reports must keep `translator_unverified=true` unless later provenance is added.

**English source:** Project Gutenberg #768 EPUB3, stored at `reference/literary/wuthering_heights/en/wuthering_heights_gutenberg_768_epub3_images.epub`, SHA256 `3F8B0EF1F30026B979A8CFB2488603ED288E75EDDEDB4407919491C47B649B89`.

Treasure Island remains prior oracle/regression material, not the active literary corpus for the next Builder track.

Status: closed for current pilot, 2026-07-08. Historical survey content below is intentionally preserved. Phase 1 covered `Treasure Island / Đảo giấu vàng`; Phase 2 covers the strongest non-Treasure candidates.

## 0. Decision frame

Goal: choose a second literary corpus, or confirm that `Treasure Island` itself can also serve as the Vietnamese reference corpus, for ref-based cross-checking in the thesis EN->VI translation workflow.

Hard constraints:

- English source must be public domain.
- Vietnamese reference must be a published, complete/unabridged translation. Abridged, adapted, children's retelling, graphic-novel, picture-book, or intermediate-language retelling editions are not acceptable as reference.
- Vietnamese translation text is copyright-protected. The thesis workflow may use a legally owned copy internally for scoring, but must not redistribute the text.
- Because the translation system emits block-aligned JSON and has coverage checks, the reference must preserve the whole work, not only story gist.

Evidence grades used here:

- **A**: directly verified chapter/table-of-contents match with the English original, or publisher/library explicitly says complete/unabridged and length is consistent.
- **B**: standard novel edition, publisher/library metadata and page count are consistent with completeness, but direct chapter count is not yet visible.
- **C**: insufficient evidence or likely abridged.
- **Reject**: explicitly abridged/adapted/graphic/picture-book/children's retelling, or materially too short.

## 1. Candidate comparison table

| Candidate | EN public-domain status | Vietnamese edition evidence | Completeness grade | Memory/xung-ho value | Length / run scope | Digitization | Leakage risk | Current decision |
|---|---|---|---|---|---|---|---|---|
| Treasure Island / Đảo giấu vàng | Published 1883; Project Gutenberg hosts public-domain text. Original PG table of contents has 34 chapters across 6 parts. | See §2. There are plausible full VN editions around 332-345 pages, but also many abridged/adapted editions. | **B candidate**, pending chapter-count check on the exact VN edition. | High for thesis continuity: same book as oracle; strong character relations, dialogue, trust/betrayal phases. | Moderate; already ingested/oracled in main track. | EN easy via Gutenberg; VN likely requires bought print/OCR unless official ebook exists. | Medium-high: famous classic likely in model pretraining. | Conditional #1 if a 34-chapter VN edition is verified. |
| Pride and Prejudice / Kiêu hãnh và định kiến | Published 1813; Project Gutenberg hosts public-domain text and marks public domain in USA. Original benchmark: 61 chapters. | Diệp Minh Tâm / Liên Việt / NXB Văn Học 2016, 523 pages; older Diệp Minh Tâm bìa cứng 602 pages; Thu Trinh 525 pages. No direct public TOC proof yet. | **B candidate** | Very high: dense dialogue, family hierarchy, courtship, honorific/register pressure. | Medium-long; feasible full run, but longer than Gatsby/Treasure. | EN easy; VN likely print/OCR unless legal ebook. | High: canonical global classic, many VN translations. | Strong candidate, but edition must be physically chapter-checked. |
| Jane Eyre / Jên Erơ | Published 1847; Project Gutenberg hosts public-domain text. Original benchmark: 38 chapters. | Đông A / NXB Văn học 2026, Thanh Loan translation, 624 pages, ISBN listed; Nhã Nam / NXB Văn học 2023, Trịnh Y Thư translation, 540 pages. | **B+ candidate** | Very high: first-person arc, class, governess/master address, religion/family phases. | Long; full run cost higher, but manageable as subset/full with budget. | EN easy; VN print/OCR likely. | High. | Top-3 candidate; strongest if we want xưng-hô/relationship stress. |
| Wuthering Heights / Đồi gió hú | Published 1847; Project Gutenberg hosts public-domain text. Original benchmark: 34 chapters. | Nhã Nam / NXB Văn Học 2024, Dương Tường translation, 489 pages; older library record has Dương Tường, 505 pages. | **B+ candidate** | Very high: generational relations, revenge, class, hostile dialogue, nested narration. | Medium; feasible full run. | EN easy; VN print/OCR likely. | High. | Top-3 candidate; best balance of memory difficulty and manageable length. |
| The Great Gatsby / Đại gia Gatsby | Published 1925; Project Gutenberg release 2021 and marks public domain in USA. Original benchmark: 9 chapters. | Nhã Nam / NXB Hội Nhà Văn 2022, Trịnh Lữ translation, 252-260 pages. | **B+ candidate** | Medium: strong voice/style and social register, but short and fewer long-running relation phases. | Short; very cheap full run. | EN easy; VN print/OCR likely, possibly easier due short length. | Very high: famous and recent PD; likely leakage. | Good control corpus, but weaker than Bronte/Austen for memory/xưng-hô. |
| The Call of the Wild / Tiếng gọi nơi hoang dã | TBD phase 3 | TBD phase 3 | TBD | TBD | TBD | TBD | TBD | Pending |
| White Fang / Nanh trắng | TBD phase 3 | TBD phase 3 | TBD | TBD | TBD | TBD | TBD | Pending |
| Tom Sawyer / Huckleberry Finn | TBD phase 3 | TBD phase 3 | TBD | TBD | TBD | TBD | TBD | Pending |
| Sherlock Holmes collections | TBD phase 3 | TBD phase 3 | TBD | Lower priority: short-story collection, not a single novel. | TBD | TBD | TBD | Pending |

## 2. Treasure Island / Đảo giấu vàng

### 2.1 English source

`Treasure Island` is a safe English-source candidate for the public-domain constraint.

- Project Gutenberg lists `Treasure Island` by Robert Louis Stevenson and provides the full text. Source: [Project Gutenberg ebook 120](https://www.gutenberg.org/ebooks/120).
- The Gutenberg HTML table of contents shows 34 numbered chapters: Part One chapters I-VI; Part Two VII-XII; Part Three XIII-XV; Part Four XVI-XXI; Part Five XXII-XXVII; Part Six XXVIII-XXXIV. Source: [Gutenberg HTML, table of contents](https://www.gutenberg.org/files/120/120-h/120-h.htm).

Completeness benchmark for any Vietnamese edition: it should map to **34 chapters** or provide equivalent unabridged continuity.

### 2.2 Vietnamese editions found

| Edition / line | Metadata found | Evidence | Completeness judgment |
|---|---|---|---|
| **Nguỵ Thanh Tuyên translation, NXB Văn học, 2023** | National Library monthly bibliography lists: `Đảo giấu vàng : Tiểu thuyết / Robert Louis Stevenson ; Nguỵ Thanh Tuyên dịch. - Tái bản lần thứ 1. - H. : Văn học, 2023. - 345 tr. ; 21 cm. - 89000đ. - 1000b. Tên sách tiếng Anh: Treasure island`. Source: [Thư mục quốc gia tháng 7/2023, item 2063](https://nlv.gov.vn/dmdocuments/tmqg-07-2023.pdf). | Library metadata calls it a novel and page count 345 is consistent with a complete prose translation. No visible chapter-count proof yet. | **B candidate**. Strongest metadata so far; needs physical/preview TOC check for 34 chapters before final acceptance. |
| **Ace Lê translation, Nhã Nam / NXB Hội Nhà Văn, 2020** | Nhã Nam product page lists author R. L. Stevenson, translator Ace Lê, NXB Hội Nhà Văn, 332 pages, release 2020. Source: [Nhã Nam product page](https://nhanam.vn/dao-giau-vang-nha-nam). | Standard prose-novel page count is consistent with completeness. Product page does not expose TOC/chapter count or say unabridged. | **B candidate**. Worth checking if a copy or preview confirms 34 chapters. |
| **Đông A / NXB Văn Học, 2020 reprint** | Khai Tâm page lists Đông A, NXB Văn Học, 335 pages, April 2020. Source: [Khai Tâm product page](https://khaitam.com/products/dao-giau-vang-1). A Đông A catalog mirror lists `Đảo giấu vàng - TB 2020`, ISBN-like code 8936071677884, NXB Văn Học, 336 pages. Source: [Scribd catalog mirror](https://www.scribd.com/document/630141005/Danh-m%E1%BB%A5c-sach-%C4%90ong-A-%C4%91%E1%BB%99c-quy%E1%BB%81n). | Page count is consistent with a full prose translation; source quality for the catalog mirror is weaker than publisher/library metadata. | **B-/C+ candidate** until publisher/TOC or library record confirms translator and chapter count. |
| **Vương Đăng translation, NXB Văn hóa Thông tin / Phương Đông, 2010** | Ebook metadata page lists translator Vương Đăng, NXB Văn hóa Thông tin, Phương Đông, 352 pages, release 12/2010. Source: [dtv-ebook metadata](https://dtv-ebook.com.vn/dao-giau-vang-dao-chau-bau_1398.html). | Page count is plausible, but source is an ebook redistribution/metadata page, not publisher/library record. | **C+ candidate**. Needs independent publisher/library or physical TOC check. |
| **Vũ Ngọc Phan translation / Minh Long, NXB Văn học, 2018** | A library announcement lists Vũ Ngọc Phan translation, NXB Văn học / Minh Long, 275 pages. Source: [Thư viện Hải Phòng notice, item 192](https://www.thuvienhaiphong.org.vn/tin-tuc/thu-muc-thong-bao-sach-moi-thang-12-2019). Tiki listing also identifies Vũ Ngọc Phan as translator for ISBN 8936067595512. Source: [Tiki listing](https://tiki.vn/sach-dao-giau-vang-p32636660.html). | A forum discussion claims one Vũ Ngọc Phan line is 30 chapters vs original 34 and has omitted details / Vietnamized names. Source: [TVE-4U discussion](https://tve-4u.org/threads/dao-giau-vang-robert-louis-stevenson.9596/). Forum evidence is not official, but it is enough to flag risk. | **Do not use unless physically verified**. Likely abridged or at least risky for block-aligned reference. |
| **An Lạc Group / Huy Hoàng, 2020 or 2024 school/classic line** | Huy Hoàng page lists An Lạc Group, NXB Mỹ Thuật, 144 pages, 17x24. Source: [Huy Hoàng product page](https://www.huyhoangbook.vn/products/dao-giau-vang-1). Tiki `Danh tác trong nhà trường` listing also shows An Lạc Group, NXB Văn Học, 144 pages. Source: [Tiki listing](https://tiki.vn/sach-danh-tac-trong-nha-truong-dao-giau-vang-huy-hoang-p277856957.html). | 144 pages with school/children packaging is too short for a reliable full reference. | **Reject**. Likely abridged/adapted. |
| **Kim Đồng `Danh tác thế giới` graphic version** | Kim Đồng lists 208 pages, mixed authors including Neung In Publishing Company / W. Shakespeare / R. L. Stevenson, and explicitly describes the story as converted to comics with short dialogue. Source: [Kim Đồng `Danh tác thế giới`](https://nxbkimdong.com.vn/danh-tac-the-gioi-dao-giau-vang-1). | Explicit graphic adaptation. | **Reject**. Not a prose full translation. |
| **Kim Đồng `Danh tác muôn thuở` picture-book version** | Kim Đồng lists 36 pages, authors Robert Louis Stevenson, Antonis Papatheodoulou, Iris Samartzi; series description says the classics are selected and retold briefly with illustrations. Source: [Kim Đồng `Danh tác muôn thuở`](https://nxbkimdong.com.vn/danh-tac-muon-thuo-dao-giau-vang-hay-truyen-cuop-bien-li-ki-nhat). | Explicit picture-book retelling. | **Reject**. Not full translation. |

### 2.3 Interim conclusion for Treasure Island

There **likely exists at least one full Vietnamese prose edition** suitable for the reference role, but the current web evidence is not yet Grade A because I have not found a public TOC/preview proving a 34-chapter match.

Best candidates to verify physically or via legal preview:

1. **Nguỵ Thanh Tuyên, NXB Văn học, 2023, 345 pages**. This has the strongest library metadata and should be checked first.
2. **Ace Lê, Nhã Nam / NXB Hội Nhà Văn, 2020, 332 pages**. Strong publisher metadata; needs TOC/chapter check.
3. **Đông A / NXB Văn học, 2020, 335/336 pages**. Plausible, but needs stronger metadata on translator and TOC.

If any of these confirms 34 chapters, `Treasure Island` becomes the strongest candidate because one corpus can serve both roles: existing oracle + Vietnamese reference. If not, move to Phase 2 candidates.

### 2.4 Memory/xưng-hô suitability

`Treasure Island` remains valuable for memory/xưng-hô testing:

- Stable cast: Jim Hawkins, Long John Silver, Dr. Livesey, Squire Trelawney, Captain Smollett, Ben Gunn, pirates.
- Relationship phases: trust -> suspicion -> betrayal -> tactical alliance -> moral reckoning.
- Dialogue density: high enough to expose address/register issues.
- Limitation: protagonist narration and adventure genre are less socially dense than Austen/Bronte, so it is not the maximum-stress test for xưng-hô.

### 2.5 Leakage risk

Leakage risk is medium-high: `Treasure Island` is a famous public-domain classic and likely appears in model pretraining, and Vietnamese editions are also culturally familiar. This does not disqualify it, but final evaluation should defend with ablation/delta: compare memory-on vs memory-off rather than claiming absolute novelty.

## 3. Pride and Prejudice / Kiêu hãnh và định kiến

### 3.1 English source

`Pride and Prejudice` is safe for the public-domain source constraint.

- Project Gutenberg hosts Jane Austen's `Pride and Prejudice` and marks it public domain in the United States. Source: [Project Gutenberg ebook 1342](https://www.gutenberg.org/ebooks/1342).
- The Gutenberg HTML text includes the full novel and reaches Chapter 61. Source: [Gutenberg HTML](https://www.gutenberg.org/files/1342/1342-h/1342-h.htm).

Completeness benchmark: a Vietnamese reference should preserve **61 chapters**.

### 3.2 Vietnamese editions found

| Edition / line | Metadata found | Evidence | Completeness judgment |
|---|---|---|---|
| **Diệp Minh Tâm translation, Liên Việt / NXB Văn Học, 2016** | Book365 lists author Jane Austen, translator Diệp Minh Tâm, NXB Văn Học, 523 pages, dimensions 15.5x23.5 cm. Source: [Book365 listing](https://book365.vn/sach/117887_kieu-hanh-va-dinh-kien). | 523 pages is consistent with a full translation. No public TOC/chapter count visible yet. | **B candidate**. Strong enough to shortlist, but must verify 61 chapters from copy/preview. |
| **Diệp Minh Tâm translation, bìa cứng line, 602 pages** | A bookstore listing identifies Diệp Minh Tâm as translator and gives 602 pages. Source: [Minh Long Book listing](https://minhlongbook.vn/products/kieu-hanh-va-dinh-kien-bia-cung). | Larger page count strongly suggests unabridged, but product metadata alone is not direct proof. | **B candidate**. Useful if a physical copy can be obtained; chapter check still required. |
| **Thu Trinh translation, 525 pages** | Another listing identifies Thu Trinh as translator and 525 pages. Source: [Newshop listing](https://newshop.vn/kieu-hanh-va-dinh-kien.html). | Page count is plausible for full text; translation line differs, so it should be treated as a separate candidate. | **B-/C+** until publisher/library metadata and chapter count are verified. |

### 3.3 Interim assessment

`Pride and Prejudice` is one of the best **xưng-hô / social-register stress tests**: family hierarchy, courtship, status asymmetry, irony, and repeated dialogue across changing relationships. The main weakness is not suitability but verification: I have not yet found a public TOC proving a Vietnamese edition has all 61 chapters. If a Diệp Minh Tâm edition can be physically checked, it is a serious top-3 candidate.

## 4. Jane Eyre / Jên Erơ

### 4.1 English source

`Jane Eyre` is safe for the public-domain source constraint.

- Project Gutenberg hosts Charlotte Bronte's `Jane Eyre`. Source: [Project Gutenberg ebook 1260](https://www.gutenberg.org/ebooks/1260).
- The Gutenberg HTML text includes Chapter XXXVIII, so the benchmark is **38 chapters**. Source: [Gutenberg HTML](https://www.gutenberg.org/files/1260/1260-h/1260-h.htm).

### 4.2 Vietnamese editions found

| Edition / line | Metadata found | Evidence | Completeness judgment |
|---|---|---|---|
| **Thanh Loan translation, Đông A / NXB Văn học, 2026** | Đông A listing gives author Charlotte Bronte, translator Thanh Loan, NXB Văn học, 624 pages, 16x24 cm, ISBN 8936203365764. Source: [Đông A listing](https://dongabooks.vn/products/jane-eyre). | 624 pages is consistent with a full translation of a long novel. No public TOC visible in the listing. | **B+ candidate**. Strong edition metadata; needs 38-chapter TOC check before final acceptance. |
| **Trịnh Y Thư translation, Nhã Nam / NXB Văn học, 2023** | Nhã Nam lists translator Trịnh Y Thư, NXB Văn Học, 540 pages, release 2023. Source: [Nhã Nam listing](https://nhanam.vn/jane-eyre). | 540 pages is plausible for full text; this is a publisher page. | **B+ candidate**. Also needs chapter-count proof. |

### 4.3 Interim assessment

`Jane Eyre` is probably the strongest **memory/xưng-hô stress test** among Phase 2 candidates. It has first-person narration, class asymmetry, employer/governess address, kinship discovery, religious/moral language, and long relationship arcs. The tradeoff is length: it is more expensive to run than `Treasure Island`, `Wuthering Heights`, or `Gatsby`.

## 5. Wuthering Heights / Đồi gió hú

### 5.1 English source

`Wuthering Heights` is safe for the public-domain source constraint.

- Project Gutenberg hosts Emily Bronte's `Wuthering Heights`. Source: [Project Gutenberg ebook 768](https://www.gutenberg.org/ebooks/768).
- The Gutenberg HTML text reaches Chapter XXXIV, so the benchmark is **34 chapters**. Source: [Gutenberg HTML](https://www.gutenberg.org/files/768/768-h/768-h.htm).

### 5.2 Vietnamese editions found

| Edition / line | Metadata found | Evidence | Completeness judgment |
|---|---|---|---|
| **Dương Tường translation, Nhã Nam / NXB Văn Học, 2024** | Nhã Nam lists translator Dương Tường, NXB Văn Học, 489 pages, release 2024. Source: [Nhã Nam listing](https://nhanam.vn/doi-gio-hu). | Publisher metadata + page count are consistent with a full prose translation. No public TOC visible yet. | **B+ candidate**. Strong shortlist item; check 34 chapters before final acceptance. |
| **Dương Tường translation, older 505-page line** | Thư viện Hải Phòng announcement lists `Đồi gió hú`, Dương Tường translation, NXB Văn học, 505 pages. Source: [Thư viện Hải Phòng notice](https://www.thuvienhaiphong.org.vn/tin-tuc/thu-muc-thong-bao-sach-moi-thang-10-2018). | Library metadata supports that this Dương Tường line is a substantial full-novel edition, but no TOC. | **B candidate**. Useful corroborating metadata. |

### 5.3 Interim assessment

`Wuthering Heights` is a high-value literary test: nested narration, hostile dialogue, family/class conflict, love/revenge phases, and generational relationship changes. Compared with `Jane Eyre`, it is shorter and likely cheaper to run while still stressing memory. This is currently the best **balance** candidate if `Treasure Island` cannot serve as the reference corpus.

## 6. The Great Gatsby / Đại gia Gatsby

### 6.1 English source

`The Great Gatsby` is now usable under the English public-domain constraint in the United States.

- Project Gutenberg hosts F. Scott Fitzgerald's `The Great Gatsby` and marks it public domain in the United States. Source: [Project Gutenberg ebook 64317](https://www.gutenberg.org/ebooks/64317).
- The original novel is structured in **9 chapters**; Project Gutenberg's HTML text is the full novel. Source: [Gutenberg HTML](https://www.gutenberg.org/cache/epub/64317/pg64317-images.html).

### 6.2 Vietnamese editions found

| Edition / line | Metadata found | Evidence | Completeness judgment |
|---|---|---|---|
| **Trịnh Lữ translation, Nhã Nam / NXB Hội Nhà Văn, 2022** | Nhã Nam lists translator Trịnh Lữ, NXB Hội Nhà Văn, 252 pages, release 2022. Source: [Nhã Nam listing](https://nhanam.vn/dai-gia-gatsby). | Publisher metadata and page count are consistent with a complete translation of a short novel. No public TOC/chapter count visible. | **B+ candidate**. A very practical reference if a 9-chapter copy check passes. |
| **Trịnh Lữ translation, 260-page trade listings** | Several bookstore listings give 260 pages for the Trịnh Lữ line. Source: [FAHASA listing](https://www.fahasa.com/dai-gia-gatsby-tai-ban-2021.html). | Corroborates standard-length edition, but bookstore metadata varies slightly in page count. | **B candidate**. Same line should be checked by actual copy. |

### 6.3 Interim assessment

`The Great Gatsby` is attractive as a **cheap control corpus**: short, stylistically strong, and easy to run end-to-end. It is weaker than Austen/Bronte for memory/xưng-hô because it has fewer sustained address-policy phases and a smaller cast. Leakage risk is also high. It is a good secondary control, not the best main literary stress test.

## 7. Phase 2 ranking update

Current top candidates after Phase 2:

1. **Treasure Island / Đảo giấu vàng**, if a 34-chapter VN edition is physically verified. It uniquely reuses the existing oracle corpus.
2. **Wuthering Heights / Đồi gió hú**, Dương Tường translation, Nhã Nam/NXB Văn Học. Best balance of literary memory pressure and manageable length.
3. **Jane Eyre / Jên Erơ**, Thanh Loan or Trịnh Y Thư translation. Strongest xưng-hô stress test, but longer.
4. **Pride and Prejudice / Kiêu hãnh và định kiến**. Very strong social-register corpus, but current web evidence needs a cleaner publisher/library/chapter proof.
5. **The Great Gatsby / Đại gia Gatsby**. Best cheap control, not best memory stress test.

## 8. Next phase plan

Phase 3 will screen the lower-priority or riskier candidates:

- `The Call of the Wild / Tiếng gọi nơi hoang dã`
- `White Fang / Nanh trắng`
- `Tom Sawyer / Những cuộc phiêu lưu của Tom Sawyer`
- `Huckleberry Finn / Những cuộc phiêu lưu của Huckleberry Finn`
- `Sherlock Holmes` collections

## Source ledger

- Project Gutenberg, `Treasure Island` ebook page: <https://www.gutenberg.org/ebooks/120>
- Project Gutenberg, `Treasure Island` HTML TOC/full text: <https://www.gutenberg.org/files/120/120-h/120-h.htm>
- National Library of Vietnam, `Thư mục quốc gia tháng 7/2023`, item 2063: <https://nlv.gov.vn/dmdocuments/tmqg-07-2023.pdf>
- Nhã Nam product page, `ĐẢO GIẤU VÀNG`: <https://nhanam.vn/dao-giau-vang-nha-nam>
- Khai Tâm product page, `Đảo Giấu Vàng - Tái bản 2020`: <https://khaitam.com/products/dao-giau-vang-1>
- Huy Hoàng product page, `Đảo giấu vàng (Tái bản 2020)`: <https://www.huyhoangbook.vn/products/dao-giau-vang-1>
- Kim Đồng product page, `Danh tác thế giới - Đảo giấu vàng`: <https://nxbkimdong.com.vn/danh-tac-the-gioi-dao-giau-vang-1>
- Kim Đồng product page, `Danh tác muôn thuở - Đảo giấu vàng`: <https://nxbkimdong.com.vn/danh-tac-muon-thuo-dao-giau-vang-hay-truyen-cuop-bien-li-ki-nhat>
- Thư viện Hải Phòng notice, `Thư mục thông báo sách mới tháng 12/2019`: <https://www.thuvienhaiphong.org.vn/tin-tuc/thu-muc-thong-bao-sach-moi-thang-12-2019>
- Tiki listing, Vũ Ngọc Phan edition: <https://tiki.vn/sach-dao-giau-vang-p32636660.html>
- Tiki listing, An Lạc Group school edition: <https://tiki.vn/sach-danh-tac-trong-nha-truong-dao-giau-vang-huy-hoang-p277856957.html>
- TVE-4U discussion, abridgment risk for older edition: <https://tve-4u.org/threads/dao-giau-vang-robert-louis-stevenson.9596/>
- Project Gutenberg, `Pride and Prejudice` ebook page: <https://www.gutenberg.org/ebooks/1342>
- Project Gutenberg, `Pride and Prejudice` HTML text: <https://www.gutenberg.org/files/1342/1342-h/1342-h.htm>
- Book365, `Kiêu hãnh và định kiến`, Diệp Minh Tâm / NXB Văn Học line: <https://book365.vn/sach/117887_kieu-hanh-va-dinh-kien>
- Minh Long Book, `Kiêu hãnh và định kiến - bìa cứng`: <https://minhlongbook.vn/products/kieu-hanh-va-dinh-kien-bia-cung>
- Newshop, `Kiêu Hãnh Và Định Kiến`: <https://newshop.vn/kieu-hanh-va-dinh-kien.html>
- Project Gutenberg, `Jane Eyre` ebook page: <https://www.gutenberg.org/ebooks/1260>
- Project Gutenberg, `Jane Eyre` HTML text: <https://www.gutenberg.org/files/1260/1260-h/1260-h.htm>
- Đông A, `Jane Eyre`: <https://dongabooks.vn/products/jane-eyre>
- Nhã Nam, `Jane Eyre`: <https://nhanam.vn/jane-eyre>
- Project Gutenberg, `Wuthering Heights` ebook page: <https://www.gutenberg.org/ebooks/768>
- Project Gutenberg, `Wuthering Heights` HTML text: <https://www.gutenberg.org/files/768/768-h/768-h.htm>
- Nhã Nam, `Đồi gió hú`: <https://nhanam.vn/doi-gio-hu>
- Thư viện Hải Phòng notice, `Thư mục thông báo sách mới tháng 10/2018`: <https://www.thuvienhaiphong.org.vn/tin-tuc/thu-muc-thong-bao-sach-moi-thang-10-2018>
- Project Gutenberg, `The Great Gatsby` ebook page: <https://www.gutenberg.org/ebooks/64317>
- Project Gutenberg, `The Great Gatsby` HTML text: <https://www.gutenberg.org/cache/epub/64317/pg64317-images.html>
- Nhã Nam, `Đại gia Gatsby`: <https://nhanam.vn/dai-gia-gatsby>
- FAHASA, `Đại gia Gatsby - tái bản 2021`: <https://www.fahasa.com/dai-gia-gatsby-tai-ban-2021.html>
