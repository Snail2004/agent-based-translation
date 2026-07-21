# Canonical Source Rich Fixture V1

This directory is a deterministic, book-neutral source corpus for testing the
Canonical Source Package V1 ingest contract. It is authored in-repo; no test
depends on downloading mutable content from the internet.

The fixture has four equivalent source variants:

- `source.epub`: the primary all-in-one rich document.
- `source.html`: rich HTML with local media, MathML, tables, and code.
- `source.md`: Markdown with local media, TeX math, tables, code, and raw HTML.
- `source.txt`: the text-only degradation case.

`fixture_contract.json` records the minimum structural and preservation
coverage expected from each format. The contract intentionally compares
semantic coverage rather than requiring byte-identical normalized documents,
because each source format carries different structural capabilities.

The EPUB is built deterministically from `epub_src/` by `build_epub.py`. The
checked-in `source.epub` is the artifact consumed by tests and can be rebuilt
with:

```powershell
python build_epub.py
```

The fixture covers front matter, two content chapters, back matter, headings,
paragraphs, dialogue-like quotations, lists, links, footnotes, figures,
captions, a chart, a table, inline and display equations, source code, CSS, and
an image without alternative text. TXT can only exercise text structure and
does not claim embedded-asset preservation.
