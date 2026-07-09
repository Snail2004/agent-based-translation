# Wuthering Heights / Doi gio hu source manifest

## English source

- Title: Wuthering Heights
- Author: Emily Bronte
- Source: Project Gutenberg ebook #768
- Format downloaded: EPUB3 with images
- URL: https://www.gutenberg.org/ebooks/768.epub3.images
- Local file: `en/wuthering_heights_gutenberg_768_epub3_images.epub`
- SHA256: `3F8B0EF1F30026B979A8CFB2488603ED288E75EDDEDB4407919491C47B649B89`
- Size: 587403 bytes
- Downloaded: 2026-07-08
- Use: public-domain English source for literary Builder pilot.
- EPUB sanity check:
  - ZIP entries: 46
  - MIME type: `application/epub+zip`
  - TOC files: `OEBPS/toc.xhtml`, `OEBPS/toc.ncx`
  - TOC chapter count: 34 (`CHAPTER I` through `CHAPTER XXXIV`)
  - Footer/license item is separate and must not be ingested as a chapter.

## Vietnamese reference candidate

- Title: Doi gio hu
- Local file: `vi/doi_gio_hu_vi_full_34ch_candidate.epub`
- SHA256: `6737B7DC333C7795A5BB6987274C78C27425DA8B842C89295AA62C2B5B4B84BE`
- Size: 675622 bytes
- Source status: user-supplied legal copy, 2026-07-08.
- Reference status: **accepted as full-structure Vietnamese reference candidate**.
- Translator/edition status: **unverified in EPUB metadata**.
- Metadata in EPUB:
  - Title: `Doi Gio Hu` / `Doi Gio Hu` with Vietnamese text title rendered as Doi gio hu in this ASCII manifest.
  - Creator: Emily Bronte
  - Publisher: Van Hoc
  - Language field: `en` (incorrect; content is Vietnamese)
  - Contributor: calibre
- EPUB sanity check:
  - ZIP entries: 47
  - MIME type: `application/epub+zip`
  - TOC items: introduction + **34 numbered chapters** + appendix
  - Chapter 1 spot check against Gutenberg EN:
    - EN chapter 1: 11264 chars / 1965 English words
    - VI chapter 1: 11649 chars / 2644 rough Vietnamese tokens
    - Key content anchors present: `1801`, landlord visit, Heathcliff, Joseph, dogs scene, Wuthering Heights explanation, final Lockwood sociability contrast.
    - Named entity counts align for chapter 1: Heathcliff 8/8, Lockwood 3/3, Joseph 4/4, Earnshaw 1/1.
- Use: private evaluation/reference alignment only. Do not redistribute.

### Published Vietnamese edition target

- Translator: Duong Tuong
- Publisher: NXB Van Hoc / Nha Nam
- Public metadata sources:
  - Nha Nam product page: https://nhanam.vn/doi-gio-hu
  - Hanoi Academy library record: https://thuvien.hanoiacademy.edu.vn/courses/doi-gio-hu-emily-bronte/
- Public metadata observed:
  - Nha Nam page: translator Duong Tuong, publisher Van Hoc, 489 pages, release year 2024.
  - Hanoi Academy library record: translator Duong Tuong, Van Hoc / Nha Nam, 2023, 489 pages, ISBN `9786043724981`.
- Caveat: the local EPUB above does not expose Duong Tuong / Nha Nam in metadata or text credits. Treat it as full-structure candidate unless/until the exact edition is verified.

## Expected next checks

1. Run the literary ingest/scaffold on English chapters 1-4.
2. Later, align the Vietnamese candidate to English blocks for reference-based evaluation.
3. Before thesis-final gold use, record exact edition/translator provenance or keep `translator_unverified=true` in reports.
