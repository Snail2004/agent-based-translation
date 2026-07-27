# LITERARY STYLE PROFILE V1 — style dịch là lựa chọn tuyên bố trước, không phải ngẫu nhiên của model

Chốt với user 2026-07-10. Nguyên tắc lõi: **Story Bible ghi SỰ KIỆN (style-free, build một lần);
STYLE PROFILE ghi LỰA CHỌN RENDER (khoá trước khi dịch, ghi provenance).** Hai trục trực giao —
cùng một Story Bible + hai profile → hai bản dịch khác nhau nhưng mỗi bản tự nhất quán.

## 1. Phạm vi ảnh hưởng (để biết khi nào PHẢI khoá — tránh chạy lại)
- **KHÔNG ảnh hưởng:** M1 (B0-B2), M2 (B3), M3 (B4) — Builder trích sự kiện tiếng Anh, bất biến
  theo style. Mọi artifact/checkpoint Builder giữ nguyên giá trị khi đổi style.
- **ẢNH HƯỞNG — khoá profile TRƯỚC khi chạy lần đầu:**
  1. Bước LLM chốt address-policy VN (input = vocative evidence B4 + profile → bảng xưng hô
     cặp-theo-pha). ← **NHẮC: đây là mốc khoá SỚM NHẤT.**
  2. Translator prompt (khối STYLE_PROFILE trong system prompt — global, cacheable prefix).
  3. Đo lường: arm ĐƯỢC ĐO phải dùng profile khớp dịch giả tham chiếu (Dương Tường), nếu không
     điểm reference-based lẫn style-difference với error (vết gold-is-style-guide ở D2L).
     Profile khác = arm demo, không so điểm với reference.

## 2. Cơ chế (spec cho CodeX khi tới lúc — theo pattern prompt-loader hiện có)
- Profile sống trong doc này dưới dạng BLOCKQUOTE có version marker
  (`literary_style_profile_<id>_v<n>`), load bằng đúng `load_system_prompt_from_design` — chỉ
  blockquote tới được model. Đổi profile = version mới, KHÔNG sửa đè.
- Config translator thêm `style_profile_version`; run provenance ghi id này như ghi model-per-stage.
- Phân biệt: extraction prompt phải book-neutral (luật cũ); style profile là artifact PER-BOOK/
  PER-PROJECT — nhắc tên Joseph/WH trong profile là hợp lệ.

## 3. Các chiều lựa chọn của một profile (schema nội dung)
Mỗi chiều = 1 quyết định + 1-2 ví dụ ngắn. Giá trị cho profile Dương Tường: điền sau DESK-CHECK
trên bản dịch tham chiếu (task §5) — KHÔNG bịa từ trí nhớ.
1. **Xưng hô theo quan hệ + pha** (lõi): quy ước cặp (người lạ trưởng thành lịch sự; chủ–người ở;
   gia đình; vợ chồng; với trẻ con), cách xử "sir/madam", đổi xưng hô khi pha quan hệ đổi
   (Story Bible cho biết KHI NÀO đổi; profile cho biết ĐỔI THÀNH GÌ).
2. **Giọng người kể**: Lockwood-tôi (thượng lưu, mỉa) vs Nelly-tôi (bình dân, ấm) — khác register
   thế nào trong tiếng Việt.
3. **Dialect/idiolect** (Joseph): dịch thành giọng quê generic / một phương ngữ VN cụ thể / chuẩn
   hoá + giữ dấu hiệu từ vựng? (TO-VERIFY với bản tham chiếu.)
4. **Tên riêng & địa danh**: giữ nguyên (Heathcliff) vs dịch nghĩa (Wuthering Heights → "Đồi Gió Hú"
   trong văn?) vs phiên âm. (TO-VERIFY.)
5. **Danh xưng Mr./Mrs./Miss**: ông/bà/cô + dạng tên nào; "Mrs. Dean" → "bà Dean"? (TO-VERIFY.)
6. **Nhịp câu & dấu**: giữ câu dài kiểu Brontë hay ngắt; xử em-dash/chấm phẩy. (TO-VERIFY xu hướng.)
7. **Thành ngữ & mức Việt hoá**: dịch sát hình ảnh gốc vs thay thành ngữ Việt tương đương.
8. **Thán từ/cảm thán**: hệ "ôi/chao/trời đất" dùng đến đâu.

## 4. BLOCKQUOTE PROFILE (v1 — KHUNG, các giá trị TO-VERIFY sẽ điền sau desk-check §5)
> [literary_style_profile_duongtuong_v1] — DRAFT, chưa khoá. Sẽ hoàn thiện sau desk-check;
> không inject bản draft này vào bất kỳ run đo nào.

## 5. Việc còn lại trước mốc khoá (Claude chủ trì — phán đoán ngôn ngữ là việc của Claude)
- DESK-CHECK 0-API trên bản Dương Tường (epub tham chiếu, ngoài git): lấy 3-4 đoạn định vị —
  (a) Lockwood↔Heathcliff ch1 (sir, tôi–ông/ngài?), (b) một đoạn Joseph ch2/ch9 (dialect),
  (c) đoạn Nelly bắt đầu kể ch4 (giọng người kể), (d) cách viết tên nhà/địa danh — điền giá trị
  các chiều §3, chốt blockquote v1, user duyệt → LOCK.
- CodeX (sau lock): wiring loader + config + provenance theo §2, 0-API test: profile xuất hiện
  trong prompt render đúng version, đổi version → prompt đổi.

## NHẮC LỊCH (Claude tự nhắc user)
- Trước khi chạy **bước address-policy VN đầu tiên** hoặc **Translator pilot WH đầu tiên**:
  dừng, hoàn thiện §5, khoá profile. M2/M3 hiện tại KHÔNG bị chặn.

## 6. BLOCKQUOTE PROFILE — RESEARCHED PROVISIONAL V1.0

The block below is a researched, versioned candidate. It is not yet a
reference-scored Dương Tường arm: the local Vietnamese EPUB has not been
matched byte-for-byte to a verified edition. Keep `measured_arm=false` until
that provenance check and the local desk-check are complete.

> [literary_style_profile_duongtuong_researched_v1_0]
> - Prompt version: literary_style_profile_duongtuong_researched_v1_0.
> - Profile status: researched_provisional; measured_arm=false.
> - Scope: Vietnamese literary rendering for Emily Bronte's Wuthering Heights.
>   This profile controls rendering choices only. It does not extract facts,
>   resolve identity, alter relations, or repair the Story Bible.
> - Priority order: preserve source meaning and uncertainty; preserve the
>   author's narrative and character voice; produce precise, natural Vietnamese;
>   preserve rhythm and literary force; choose culturally apt wording.
> - Faithfulness and freedom: translate the complete meaning, implication,
>   tone, and social relation of the source. Creative Vietnamese choices are
>   allowed only inside that semantic and evidential boundary. Do not add,
>   omit, explain, modernize, or resolve an ambiguity merely to make prose
>   smoother.
> - Authorial style: do not flatten Emily Bronte's contrasts between reflective
>   narration, recollection, direct speech, irony, violence, tenderness, and
>   the uncanny. Preserve a distinctive voice rather than applying one generic
>   narrator voice to the whole book.
> - Narrative voices: keep Lockwood, Nelly, and other narrators recognizably
>   different in register, distance, education, warmth, and irony. Do not infer
>   a voice change from a Vietnamese phrase alone; follow the supplied narrative
>   frame and Story Bible.
> - Address and status: use the supplied address anchor as the authority for
>   speaker-addressee forms. Vietnamese pronouns, kinship terms, names, and
>   titles may vary with relationship, age, rank, intimacy, hostility, and
>   narrative phase. Never force one-to-one I/you translation, and never let
>   this profile override an anchored form without recording an
>   anchor_deviation.
> - Register: preserve social and historical distance without turning every
>   line into artificial archaic Vietnamese. Use a literary, readable register;
>   reserve colloquial, rough, or old-fashioned wording for evidence-supported
>   speakers and situations. Do not map a source dialect to a named Vietnamese
>   regional dialect unless the address/evidence packet explicitly authorizes it.
> - Idioms and culture: prefer a Vietnamese idiomatic equivalent when it
>   preserves meaning, force, and social function. Retain a salient source
>   image when replacing it would erase the author's imagery. Do not insert
>   explanatory glosses into the prose; record a genuine untranslatable gap
>   through the contract's designated field.
> - Syntax and rhythm: preserve deliberate long sentences, parallelism,
>   repetition, pauses, and abruptness when they carry voice or dramatic
>   pressure. Recast syntax only when Vietnamese grammar or comprehension
>   requires it, without flattening the cadence into uniform short sentences.
> - Lexical signature: choose exact, vivid Vietnamese words and allow a
>   distinctive choice when context supports it. Do not seek novelty, poetic
>   ornament, or unusual Sino-Vietnamese wording for its own sake. A striking
>   word must remain intelligible and must not change the scene's register.
> - Names and places: preserve person names and established place renderings
>   from the Story Bible and address anchor. Do not invent translations,
>   aliases, titles, or relationships. Keep recurring forms consistent unless
>   the supplied phase or dialogue evidence requires a change.
> - Uncertainty and auditability: preserve source ambiguity and under-
>   specification. If a responsible rendering is not supported, use the
>   contract's unresolved/deviation mechanism rather than guessing. Never use
>   later-plot knowledge to rewrite an earlier window.
> - Output discipline: return only the requested translation fields and
>   contract metadata. Do not mention this profile, the research sources, or
>   internal IDs in prose.

### 6.1 Research provenance and lock gate

The candidate above is derived from:

- Dương Tường's reported principles: a Vietnamese literary translation should
  be a reconstruction of the original; the translator is a co-author, while
  fidelity still constrains creative freedom
  ([VnExpress interview/report](https://vnexpress.net/dich-gia-duong-tuong-nguoi-dan-loi-tham-lang-1880715.html);
  [Nhan Dan discussion](https://nhandan.vn/cac-ban-tre-dung-voi-dich-post413895.html)).
- His emphasis on exact, vivid Vietnamese, cultural mediation, and careful
  research into author and context
  ([Phu Nu](https://www.phunuonline.com.vn/duong-tuong-dich-a1393094.html);
  [Tien Phong](https://tienphong.vn/dich-gia-dac-biet-post1513516.tpo);
  [Znews](https://znews.vn/duong-tuong-rua-tay-gac-kiem-van-chua-chet-chiu-voi-dich-post986341.html)).
- A published study of the first ten chapters of *Wuthering Heights* and
  *Đồi gió hú*, which reports communicative translation as the dominant
  method and documents idiomatic, syntactic, and adaptive choices
  ([Ngôn ngữ & Đời sống / VJOL PDF](https://vjol.info.vn/index.php/NNDS/article/download/19924/17497/)).

This evidence supports the profile's principles, not every individual lexical
choice. Public discussion of *Người dưng* and *Lolita* also shows why the
profile must prohibit opacity and unjustified novelty
([VnExpress on Người dưng](https://vnexpress.net/noi-ve-ban-dich-nguoi-dung-1-2-1973889.html);
 [Tuoi Tre on Lolita](https://tuoitre.vn/lolita-tro-lai-cung-cau-chuyen-dich-thuat-736151.htm)).

The current public edition anchor is Nhã Nam's 2024 listing: translator
Dương Tường, publisher Văn học, 489 pages, product code `8935235241404`
([Nhã Nam](https://nhanam.vn/doi-gio-hu)); the National Library catalogue also
lists a 2024 revised edition translated by Dương Tường
([National Library catalogue PDF](https://nlv.gov.vn/dmdocuments/tmqg-03-2025.pdf)).
The local EPUB still lacks translator/publisher credits, so this profile
remains `researched_provisional` until its content is matched to that edition
or another documented edition.

### 6.2 RESEARCHED PROVISIONAL V1.1

> [literary_style_profile_duongtuong_researched_v1_1]
> - Prompt version: literary_style_profile_duongtuong_researched_v1_1.
> - Profile status: researched_provisional; measured_arm=false.
> - Scope: Vietnamese literary rendering for Emily Bronte's Wuthering Heights.
>   This profile controls rendering choices only. It does not extract facts,
>   resolve identity, alter relations, or repair the Story Bible.
> - Priority order: preserve source meaning and uncertainty; preserve the
>   author's narrative and character voice; produce precise, natural Vietnamese;
>   preserve rhythm and literary force; choose culturally apt wording.
> - Faithfulness and freedom: translate the complete meaning, implication,
>   tone, and social relation of the source. Creative Vietnamese choices are
>   allowed only inside that semantic and evidential boundary. Do not add,
>   omit, explain, modernize, or resolve an ambiguity merely to make prose
>   smoother.
> - Authorial style: do not flatten Emily Bronte's contrasts between reflective
>   narration, recollection, direct speech, irony, violence, tenderness, and
>   the uncanny. Preserve a distinctive voice rather than applying one generic
>   narrator voice to the whole book.
> - Narrative voices: keep Lockwood, Nelly, and other narrators recognizably
>   different in register, distance, education, warmth, and irony. Do not infer
>   a voice change from a Vietnamese phrase alone; follow the supplied narrative
>   frame and Story Bible.
> - Address and status: use the supplied address anchor as the authority for
>   speaker-addressee forms. Vietnamese pronouns, kinship terms, names, and
>   titles may vary with relationship, age, rank, intimacy, hostility, and
>   narrative phase. Never force one-to-one I/you translation, and never let
>   this profile override an anchored form without recording an
>   anchor_deviation.
> - Unknown relationship: when the supplied evidence does not establish the
>   relationship between speaker and addressee, choose the most neutral or
>   distant form the evidence still supports, and record the choice through
>   the contract's deviation/annotation field. Do not select a warmer, closer,
>   or more familiar form because it reads more naturally.
> - Register: preserve social and historical distance without turning every line
>   into artificial archaic Vietnamese. Use a literary, readable register;
>   reserve colloquial, rough, or old-fashioned wording for evidence-supported
>   speakers and situations. Do not map a source dialect to a named Vietnamese
>   regional dialect unless the address/evidence packet explicitly authorizes it.
> - Idioms and culture: prefer a Vietnamese idiomatic equivalent when it
>   preserves meaning, force, and social function. Retain a salient source
>   image when replacing it would erase the author's imagery. Do not insert
>   explanatory glosses into the prose; record a genuine untranslatable gap
>   through the contract's designated field.
> - Syntax and rhythm: preserve deliberate long sentences, parallelism,
>   repetition, pauses, and abruptness when they carry voice or dramatic
>   pressure. Recast syntax only when Vietnamese grammar or comprehension
>   requires it, without flattening the cadence into uniform short sentences.
> - Lexical signature: choose exact, vivid Vietnamese words and allow a
>   distinctive choice when context supports it. Do not seek novelty, poetic
>   ornament, or unusual Sino-Vietnamese wording for its own sake. A striking
>   word must remain intelligible and must not change the scene's register.
> - Names and places: preserve person names and established place renderings
>   from the Story Bible and address anchor. Do not invent translations,
>   aliases, titles, or relationships. Keep recurring forms consistent unless
>   the supplied phase or dialogue evidence requires a change.
> - Uncertainty and auditability: preserve source ambiguity and under-
>   specification. If a responsible rendering is not supported, use the
>   contract's unresolved/deviation mechanism rather than guessing. Never use
>   later-plot knowledge to rewrite an earlier window.
> - Output discipline: return only the requested translation fields and
>   contract metadata. Do not mention this profile, the research sources, or
>   internal IDs in prose.
