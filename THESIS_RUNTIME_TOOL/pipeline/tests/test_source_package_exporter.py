from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.ingest.canonical_source_package import seal_asset_manifest
from pipeline.ingest.epub_normalizer import normalize_epub, write_epub_normalization
from pipeline.ingest.html_normalizer import normalize_html, write_html_normalization
from pipeline.ingest.markdown_normalizer import (
    normalize_markdown,
    write_markdown_normalization,
)
from pipeline.ingest.source_package_exporter import (
    SourcePackageExportError,
    export_source_package,
    seal_translation_overlay,
    validate_translation_overlay,
)


RICH_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "canonical_source_rich_v1"
)
CANONICAL_PACKAGE_ROOT = (
    Path(__file__).parent / "fixtures" / "canonical_source_package_v1"
)
OVERLAY_SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "ingest"
    / "schemas"
    / "canonical_translation_overlay_v1.schema.json"
)
PANDOC_AVAILABLE = shutil.which("pandoc") is not None


def _read_package(output: Path) -> tuple[dict, dict, dict]:
    return (
        json.loads((output / "document.json").read_text(encoding="utf-8")),
        json.loads(
            (output / "structure_manifest.json").read_text(encoding="utf-8")
        ),
        json.loads((output / "asset_manifest.json").read_text(encoding="utf-8")),
    )


def _html_package(tmp_path: Path) -> tuple[Path, dict, dict, dict]:
    result = normalize_html(
        RICH_FIXTURE_ROOT / "source.html",
        doc_id="p2_html_fixture",
        pandoc_executable=None,
    )
    package = tmp_path / "package"
    write_html_normalization(result, package)
    return package, *_read_package(package)


def _effective_action(unit: dict, binding: dict) -> str:
    if unit["review_required"] or unit["translation_policy"] == "review":
        return "review"
    if unit["translation_policy"] in {"preserve", "exclude"}:
        return unit["translation_policy"]
    if binding["review_required"] or binding["translation_policy"] == "review":
        return "review"
    return binding["translation_policy"]


def _complete_overlay(document: dict, structure: dict, manifest: dict) -> dict:
    unit_by_chapter = {unit["chapter_id"]: unit for unit in structure["units"]}
    binding_by_id = {
        binding["block_id"]: binding for binding in manifest["block_bindings"]
    }
    asset_by_id = {asset["asset_id"]: asset for asset in manifest["assets"]}
    translations: list[dict] = []
    for chapter in document["chapters"]:
        unit = unit_by_chapter[chapter["chapter_id"]]
        for block in chapter["blocks"]:
            block_id = block["block_id"]
            binding = binding_by_id[block_id]
            action = _effective_action(unit, binding)
            if action == "translate":
                translations.append(
                    {
                        "block_id": block_id,
                        "text": f"VI::{block_id}",
                        "html": None,
                        "markdown": None,
                    }
                )
            elif action == "translate_structured":
                protected = []
                if binding["semantic_kind"] != "table":
                    protected = [
                        asset_id
                        for asset_id in binding["asset_ids"]
                        if asset_by_id[asset_id]["kind"]
                        in {"image", "equation", "code"}
                    ]
                tokens = " ".join(
                    f"{{{{asset:{asset_id}}}}}" for asset_id in protected
                )
                if binding["semantic_kind"] == "table":
                    html_fragment = (
                        "<table><thead><tr><th>Mục</th></tr></thead>"
                        "<tbody><tr><td>Đã dịch</td></tr></tbody></table>"
                    )
                    markdown_fragment = "| Mục |\n| --- |\n| Đã dịch |"
                else:
                    html_fragment = f"<p>VI structured {tokens}</p>"
                    markdown_fragment = f"VI structured {tokens}"
                translations.append(
                    {
                        "block_id": block_id,
                        "text": f"VI structured::{block_id}",
                        "html": html_fragment,
                        "markdown": markdown_fragment,
                    }
                )
    return seal_translation_overlay(document, translations)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_overlay_schema_and_runtime_validator_accept_complete_fixture(
    tmp_path: Path,
) -> None:
    _package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(OVERLAY_SCHEMA_PATH.read_text(encoding="utf-8"))

    jsonschema.Draft202012Validator(schema).validate(overlay)
    validated = validate_translation_overlay(document, structure, manifest, overlay)

    expected = sum(
        binding["translation_policy"] in {"translate", "translate_structured"}
        for binding in manifest["block_bindings"]
        if binding["translation_policy"] != "exclude"
    )
    assert len(validated) == expected


def test_html_and_markdown_export_restore_assets_and_apply_unit_policy(
    tmp_path: Path,
) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    package_before = _tree_bytes(package)

    result = export_source_package(package, overlay, tmp_path / "export")

    html_text = result.html_path.read_text(encoding="utf-8")
    markdown_text = result.markdown_path.read_text(encoding="utf-8")
    assert "VI::p2_html_fixture" in html_text
    assert "VI::p2_html_fixture" in markdown_text
    assert "Canonical Source Rich Fixture is an authored test document" in html_text
    assert "Appendix: Preservation Notes" not in html_text
    assert "{{asset:" not in html_text
    assert "{{asset:" not in markdown_text
    assert '<img class="source-asset" src="assets/' in html_text
    assert "](assets/" in markdown_text
    assert "<table>" in html_text
    assert "| Đã dịch |" in markdown_text
    assert result.manifest["counts"]["blocks_translated"] == 12
    assert result.manifest["counts"]["blocks_structured"] == 3
    assert result.manifest["counts"]["blocks_excluded"] == 2
    assert result.manifest["counts"]["review_markers"] == 0
    assert result.manifest["counts"]["assets_copied"] == len(
        [asset for asset in manifest["assets"] if asset["availability"] == "materialized"]
    )
    assert result.manifest["artifacts"]["html"]["sha256"] == hashlib.sha256(
        result.html_path.read_bytes()
    ).hexdigest()
    assert _tree_bytes(package) == package_before


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required for MathML")
def test_translated_text_math_renders_mathml_and_markdown_keeps_exact_tex(
    tmp_path: Path,
) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    translated = next(
        row
        for row in overlay["translations"]
        if row["html"] is None and row["markdown"] is None
    )
    translated["text"] = (
        "Vecto *nghiêng* $\\mathbf{x}$, [**đậm và *lồng***], ma tran "
        "$$\\mathbf{A}=\\begin{bmatrix}a&b\\\\c&d\\end{bmatrix}$$ "
        "va he so \\(u_i\\), phep tinh a * b, va \\*literal."
    )
    exact_translated_text = translated["text"]
    overlay = seal_translation_overlay(document, overlay["translations"])
    package_before = _tree_bytes(package)

    first = export_source_package(package, overlay, tmp_path / "export_a")
    second = export_source_package(package, overlay, tmp_path / "export_b")
    html_text = first.html_path.read_text(encoding="utf-8")
    markdown_text = first.markdown_path.read_text(encoding="utf-8")

    assert html_text.count('class="source-equation-inline translated-equation"') == 2
    assert html_text.count('class="source-equation translated-equation"') == 1
    assert "vertical-align:baseline" in html_text
    assert "<em>nghiêng</em>" in html_text
    assert "<strong>đậm và <em>lồng</em></strong>" in html_text
    assert "phep tinh a * b, va *literal" in html_text
    assert html_text.count('data-renderer="pandoc-mathml"') >= 3
    assert "$\\mathbf{x}$" not in html_text
    assert "$$\\mathbf{A}" not in html_text
    assert r"\(u_i\)" not in html_text
    assert exact_translated_text in markdown_text
    assert first.manifest["rendering"]["translated_text_math"] == {
        "engine": "pandoc_mathml",
        "mathml_spans": 3,
        "unique_expressions": 3,
    }
    assert first.manifest["counts"]["translated_math_spans"] == 3
    assert _tree_bytes(first.output_dir) == _tree_bytes(second.output_dir)
    assert _tree_bytes(package) == package_before


def test_translated_text_math_fails_closed_without_pandoc(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    translated = next(
        row
        for row in overlay["translations"]
        if row["html"] is None and row["markdown"] is None
    )
    translated["text"] = r"Gia tri $x + y$ phai duoc render."
    overlay = seal_translation_overlay(document, overlay["translations"])

    with pytest.raises(SourcePackageExportError, match="requires a local Pandoc"):
        export_source_package(
            package,
            overlay,
            tmp_path / "export",
            pandoc_executable=None,
        )

    assert not (tmp_path / "export").exists()


def test_translated_text_math_rejects_malformed_delimiters(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    translated = next(
        row
        for row in overlay["translations"]
        if row["html"] is None and row["markdown"] is None
    )
    translated["text"] = r"Cong thuc bi thieu dau dong $\\frac{1}{2}."
    overlay = seal_translation_overlay(document, overlay["translations"])

    with pytest.raises(SourcePackageExportError, match="translated math is malformed"):
        export_source_package(package, overlay, tmp_path / "export")

    assert not (tmp_path / "export").exists()


def test_translation_overlay_rejects_forbidden_control_characters(
    tmp_path: Path,
) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    translated = next(
        row
        for row in overlay["translations"]
        if row["html"] is None and row["markdown"] is None
    )
    translated["text"] = "Broken ANSI math $\x1b[1mA\x1b[0m$."

    with pytest.raises(SourcePackageExportError, match="forbidden control"):
        export_source_package(package, overlay, tmp_path / "export")

    assert not (tmp_path / "export").exists()


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required for MathML")
def test_translated_math_ignores_currency_and_inline_code(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    translated = next(
        row
        for row in overlay["translations"]
        if row["html"] is None and row["markdown"] is None
    )
    translated["text"] = r"Gia $5, ma `price = $raw$`, con toan hoc la $x+1$."
    overlay = seal_translation_overlay(document, overlay["translations"])

    result = export_source_package(package, overlay, tmp_path / "export")
    html_text = result.html_path.read_text(encoding="utf-8")

    assert result.manifest["counts"]["translated_math_spans"] == 1
    assert "Gia $5" in html_text
    assert "`price = $raw$`" in html_text
    assert "$x+1$" not in html_text


def test_html_round_trip_restores_empty_alt_image_and_line_layout(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    (source_root / "images").mkdir(parents=True)
    (source_root / "images" / "cover.jpg").write_bytes(b"fixture-image")
    source = source_root / "book.html"
    source.write_text(
        """<!doctype html><html><body><article id="chapter-1">
        <h2>Chapter I</h2>
        <img src="images/cover.jpg" alt="">
        <p class="poem">First line.<br>Second line.<br><br>Final stanza.</p>
        <pre>     A shaped line
  narrows here</pre>
        <pre class="code"><code>def example():
    return 1</code></pre>
        </article></body></html>""",
        encoding="utf-8",
    )
    normalized = normalize_html(
        source,
        doc_id="layout_round_trip",
        pandoc_executable=None,
    )
    package = tmp_path / "package"
    write_html_normalization(normalized, package)
    document, structure, manifest = _read_package(package)
    overlay = _complete_overlay(document, structure, manifest)
    binding_by_id = {
        row["block_id"]: row for row in manifest["block_bindings"]
    }
    for translation in overlay["translations"]:
        source_kind = binding_by_id[translation["block_id"]]["source_kind"]
        if source_kind == "verse":
            translation["text"] = "Dòng một.\nDòng hai.\n\nKhổ cuối."
        elif source_kind == "preformatted":
            translation["text"] = "     Dòng tạo hình\n  thu hẹp ở đây"
    overlay = seal_translation_overlay(document, overlay["translations"])

    result = export_source_package(package, overlay, tmp_path / "export")
    html_text = result.html_path.read_text(encoding="utf-8")
    markdown_text = result.markdown_path.read_text(encoding="utf-8")

    assert '<img class="source-asset" src="assets/' in html_text
    assert '<p class="source-verse">Dòng một.<br>\nDòng hai.<br>\n<br>\nKhổ cuối.</p>' in html_text
    assert '<pre class="source-preformatted">     Dòng tạo hình\n  thu hẹp ở đây</pre>' in html_text
    assert '<pre class="code"><code>' in html_text
    assert "Dòng một.  \nDòng hai.  \n  \nKhổ cuối." in markdown_text
    assert "<pre>\n     Dòng tạo hình\n  thu hẹp ở đây\n</pre>" in markdown_text


def test_markdown_round_trip_preserves_heading_list_and_math_shape(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text(
        """# Opening {#opening}

> A quoted line.

- First item.
- Second item.

### Detail

Inline $x + y$ remains in this sentence.

$$z = 2$$
""",
        encoding="utf-8",
        newline="\n",
    )
    normalized = normalize_markdown(
        source,
        doc_id="markdown_round_trip",
        pandoc_executable=None,
    )
    package = tmp_path / "package"
    write_markdown_normalization(normalized, package)
    document, structure, manifest = _read_package(package)
    blocks = document["chapters"][0]["blocks"]
    asset_by_id = {asset["asset_id"]: asset for asset in manifest["assets"]}
    block_by_id = {block["block_id"]: block for block in blocks}
    block_by_source_kind = {
        binding["source_kind"]: block_by_id[binding["block_id"]]
        for binding in manifest["block_bindings"]
    }
    headings = [block for block in blocks if block["block_type"] == "heading"]
    inline_binding = next(
        binding
        for binding in manifest["block_bindings"]
        if binding["source_kind"] == "paragraph"
        and any(
            asset_by_id[asset_id]["kind"] == "equation"
            for asset_id in binding["asset_ids"]
        )
    )
    inline_block = block_by_id[inline_binding["block_id"]]
    inline_asset_id = next(
        asset_id
        for asset_id in inline_binding["asset_ids"]
        if asset_by_id[asset_id]["kind"] == "equation"
    )
    inline_token = f"{{{{asset:{inline_asset_id}}}}}"
    translations = [
        {
            "block_id": headings[0]["block_id"],
            "text": "Translated opening",
            "html": None,
            "markdown": None,
        },
        {
            "block_id": block_by_source_kind["block_quote"]["block_id"],
            "text": "Translated quotation.",
            "html": None,
            "markdown": None,
        },
        {
            "block_id": block_by_source_kind["list"]["block_id"],
            "text": "Translated list.",
            "html": "<ul><li>First translated item.</li><li>Second translated item.</li></ul>",
            "markdown": "- First translated item.\n- Second translated item.",
        },
        {
            "block_id": headings[1]["block_id"],
            "text": "Translated detail",
            "html": None,
            "markdown": None,
        },
        {
            "block_id": inline_block["block_id"],
            "text": "Translated inline equation.",
            "html": f"<p>Translated {inline_token} inline.</p>",
            "markdown": f"Translated {inline_token} inline.",
        },
    ]
    overlay = seal_translation_overlay(document, translations)

    result = export_source_package(package, overlay, tmp_path / "export")
    markdown_text = result.markdown_path.read_text(encoding="utf-8")
    html_text = result.html_path.read_text(encoding="utf-8")

    assert "# Translated opening {#opening}" in markdown_text
    assert "### Translated detail" in markdown_text
    assert "## #" not in markdown_text
    assert "- First translated item." in markdown_text
    assert "Translated $x + y$ inline." in markdown_text
    assert markdown_text.count("$$z = 2$$") == 1
    assert '<h1 id="opening">Translated opening</h1>' in html_text
    assert "<h3>Translated detail</h3>" in html_text
    if PANDOC_AVAILABLE:
        assert '<span class="source-equation-inline" data-renderer="pandoc-mathml">' in html_text
        assert '<math display="inline"' in html_text
        assert result.manifest["counts"]["equation_assets_mathml"] == 2
        assert result.manifest["counts"]["equation_assets_tex_fallback"] == 0
    else:
        assert 'data-render-status="tex-fallback"><code>$x + y$</code>' in html_text
        assert result.manifest["counts"]["equation_assets_tex_fallback"] == 2


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required for MathML")
def test_markdown_html_export_renders_mathml_and_cleans_structural_markup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "publication.md"
    source.write_text(
        """# Linear Algebra

:label:`sec-linear-algebra`

Inline $c = \\frac{5}{9}(f - 32)$ remains readable.

$$
\\mathbf{A}=\\begin{bmatrix}a&b\\\\c&d\\end{bmatrix}
$$

:eqlabel:`eq-matrix`

```{.python .input}
x = 3
print(x)
```
""",
        encoding="utf-8",
        newline="\n",
    )
    normalized = normalize_markdown(
        source,
        doc_id="publication_fixture",
        pandoc_executable=None,
    )
    package = tmp_path / "publication_package"
    write_markdown_normalization(normalized, package)
    document, structure, manifest = _read_package(package)
    overlay = _complete_overlay(document, structure, manifest)

    result = export_source_package(package, overlay, tmp_path / "publication_export")
    html_text = result.html_path.read_text(encoding="utf-8")
    markdown_text = result.markdown_path.read_text(encoding="utf-8")

    assert html_text.count('data-renderer="pandoc-mathml"') == 2
    assert '<math display="inline"' in html_text
    assert "<mfrac>" in html_text
    assert '<math display="block"' in html_text
    assert "<mtable>" in html_text
    assert '<annotation encoding="application/x-tex">' in html_text
    assert 'id="sec-linear-algebra" data-directive="label"' in html_text
    assert 'id="eq-matrix" data-directive="eqlabel"' in html_text
    assert ":label:`sec-linear-algebra`" not in html_text
    assert '<code class="language-python">x = 3\nprint(x)</code>' in html_text
    assert "```{.python .input}" not in html_text
    assert ":label:`sec-linear-algebra`" in markdown_text
    assert "```{.python .input}" in markdown_text
    assert result.manifest["rendering"]["equations"] == {
        "engine": "pandoc_mathml",
        "mathml_assets": 2,
        "tex_fallback_assets": 0,
    }

    fallback = export_source_package(
        package,
        overlay,
        tmp_path / "publication_fallback",
        pandoc_executable=None,
    )
    fallback_html = fallback.html_path.read_text(encoding="utf-8")
    assert 'data-render-status="tex-fallback"' in fallback_html
    assert fallback.manifest["counts"]["equation_assets_mathml"] == 0
    assert fallback.manifest["counts"]["equation_assets_tex_fallback"] == 2


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required for MathML")
def test_mathml_batch_falls_back_per_unsupported_equation(tmp_path: Path) -> None:
    source = tmp_path / "unsupported_math.md"
    source.write_text(
        "# Chapter\n\nGood $x + y$ and unsupported $\\unknownmacro{z}$.\n",
        encoding="utf-8",
        newline="\n",
    )
    normalized = normalize_markdown(
        source,
        doc_id="unsupported_math_fixture",
        pandoc_executable=None,
    )
    package = tmp_path / "unsupported_math_package"
    write_markdown_normalization(normalized, package)
    document, structure, manifest = _read_package(package)
    overlay = _complete_overlay(document, structure, manifest)

    result = export_source_package(package, overlay, tmp_path / "unsupported_export")
    html_text = result.html_path.read_text(encoding="utf-8")

    assert html_text.count('data-renderer="pandoc-mathml"') == 1
    assert html_text.count('data-render-status="tex-fallback"') == 1
    assert "\\unknownmacro{z}" in html_text
    assert result.manifest["counts"]["equation_assets_mathml"] == 1
    assert result.manifest["counts"]["equation_assets_tex_fallback"] == 1


def test_image_equation_asset_renders_without_text_decoding(tmp_path: Path) -> None:
    package = tmp_path / "package"
    shutil.copytree(CANONICAL_PACKAGE_ROOT, package)
    document, structure, manifest = _read_package(package)
    equation = next(
        asset for asset in manifest["assets"] if asset["kind"] == "equation"
    )
    old_path = package / str(equation["package_path"])
    old_path.unlink()
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
        "0000000b4944415478da63fcff1f0002eb01f56976650000000049454e44ae426082"
    )
    png_path = package / "assets" / "equation.png"
    png_path.write_bytes(png_bytes)
    equation.update(
        {
            "media_type": "image/png",
            "package_path": "assets/equation.png",
            "sha256": hashlib.sha256(png_bytes).hexdigest(),
            "metadata": {
                "caption": "Detected formula crop",
                "display": "block",
                "placement": "pdf_bbox_clip",
            },
        }
    )
    resealed = seal_asset_manifest(
        document,
        structure,
        assets=manifest["assets"],
        block_bindings=manifest["block_bindings"],
    )
    (package / "asset_manifest.json").write_text(
        json.dumps(resealed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    overlay = _complete_overlay(document, structure, resealed)

    result = export_source_package(package, overlay, tmp_path / "export")
    html_text = result.html_path.read_text(encoding="utf-8")
    markdown_text = result.markdown_path.read_text(encoding="utf-8")

    assert (
        '<img class="source-asset source-equation-image" '
        'src="assets/equation.png" alt="Detected formula crop">'
    ) in html_text
    assert "![Detected formula crop](assets/equation.png)" in markdown_text
    assert (result.output_dir / "assets" / "equation.png").read_bytes() == png_bytes
    assert result.manifest["rendering"]["equations"] == {
        "engine": "not_needed",
        "mathml_assets": 0,
        "tex_fallback_assets": 0,
    }


def test_export_is_byte_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)

    first = export_source_package(package, overlay, tmp_path / "first")
    second = export_source_package(package, overlay, tmp_path / "second")

    assert _tree_bytes(first.output_dir) == _tree_bytes(second.output_dir)
    assert (
        first.manifest["integrity"]["export_payload_sha256"]
        == second.manifest["integrity"]["export_payload_sha256"]
    )


@pytest.mark.parametrize("fault", ["missing", "duplicate", "foreign", "stale"])
def test_overlay_identity_and_exact_cover_fail_before_writing(
    tmp_path: Path,
    fault: str,
) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    if fault == "missing":
        overlay["translations"].pop()
    elif fault == "duplicate":
        overlay["translations"].append(dict(overlay["translations"][0]))
    elif fault == "foreign":
        overlay["translations"][0]["block_id"] = "foreign_block"
    else:
        overlay["document_sha256"] = "0" * 64

    with pytest.raises(SourcePackageExportError):
        export_source_package(package, overlay, tmp_path / "export")

    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("fault", ["missing_token", "duplicate_token", "unsafe_html"])
def test_structured_translation_cannot_drop_assets_or_inject_document_markup(
    tmp_path: Path,
    fault: str,
) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    structured = next(
        row
        for row in overlay["translations"]
        if isinstance(row["html"], str) and "{{asset:" in row["html"]
    )
    token = "{{asset:" + structured["html"].split("{{asset:", 1)[1].split("}}", 1)[0] + "}}"
    if fault == "missing_token":
        structured["html"] = structured["html"].replace(token, "")
    elif fault == "duplicate_token":
        structured["markdown"] += " " + token
    else:
        structured["html"] = "<script>alert(1)</script>" + structured["html"]

    with pytest.raises(SourcePackageExportError):
        export_source_package(package, overlay, tmp_path / "export")

    assert not (tmp_path / "export").exists()


def test_review_rows_fail_by_default_and_can_only_render_visible_markers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "uncertain.md"
    source.write_text(
        "A document without a reliable chapter heading.\n\nSecond paragraph.\n",
        encoding="utf-8",
    )
    normalized = normalize_markdown(
        source,
        doc_id="review_fixture",
        pandoc_executable=None,
    )
    package = tmp_path / "review_package"
    write_markdown_normalization(normalized, package)
    document, structure, manifest = _read_package(package)
    overlay = _complete_overlay(document, structure, manifest)

    with pytest.raises(SourcePackageExportError, match="unresolved review rows"):
        export_source_package(package, overlay, tmp_path / "strict")
    marked = export_source_package(
        package,
        overlay,
        tmp_path / "marked",
        review_mode="markers",
    )

    assert "Review required" in marked.html_path.read_text(encoding="utf-8")
    assert "[!WARNING]" in marked.markdown_path.read_text(encoding="utf-8")
    assert marked.manifest["counts"]["review_markers"] == 2
    assert marked.manifest["unresolved"]


def test_missing_bound_asset_is_never_silently_omitted(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    binding = next(
        row
        for row in manifest["block_bindings"]
        if row["translation_policy"] == "preserve"
        and any(
            asset["asset_id"] in row["asset_ids"] and asset["kind"] == "image"
            for asset in manifest["assets"]
        )
    )
    asset_id = next(
        asset_id
        for asset_id in binding["asset_ids"]
        if next(asset for asset in manifest["assets"] if asset["asset_id"] == asset_id)[
            "kind"
        ]
        == "image"
    )
    asset = next(asset for asset in manifest["assets"] if asset["asset_id"] == asset_id)
    asset["availability"] = "missing"
    asset["package_path"] = None
    asset["sha256"] = None
    asset["review_required"] = True
    resealed = seal_asset_manifest(
        document,
        structure,
        assets=manifest["assets"],
        block_bindings=manifest["block_bindings"],
    )
    (package / "asset_manifest.json").write_text(
        json.dumps(resealed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SourcePackageExportError, match="unresolved review rows"):
        export_source_package(package, overlay, tmp_path / "strict")
    marked = export_source_package(
        package,
        overlay,
        tmp_path / "marked",
        review_mode="markers",
    )

    assert asset_id in marked.html_path.read_text(encoding="utf-8")
    assert marked.manifest["unresolved"]


def test_unbound_missing_inventory_asset_still_fails_closed(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    bound_ids = {
        asset_id
        for binding in manifest["block_bindings"]
        for asset_id in binding["asset_ids"]
    }
    asset = next(
        asset for asset in manifest["assets"] if asset["asset_id"] not in bound_ids
    )
    asset["availability"] = "missing"
    asset["package_path"] = None
    asset["sha256"] = None
    asset["review_required"] = True
    resealed = seal_asset_manifest(
        document,
        structure,
        assets=manifest["assets"],
        block_bindings=manifest["block_bindings"],
    )
    (package / "asset_manifest.json").write_text(
        json.dumps(resealed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SourcePackageExportError, match=asset["asset_id"]):
        export_source_package(package, overlay, tmp_path / "export")


def test_unsafe_preserved_html_fragment_is_not_executed(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.md"
    source.write_text(
        "# Chapter I\n\nTranslate this paragraph.\n\n"
        "<aside onclick='alert(1)'>Stored note</aside>\n",
        encoding="utf-8",
    )
    normalized = normalize_markdown(
        source,
        doc_id="unsafe_fragment_fixture",
        pandoc_executable=None,
    )
    package = tmp_path / "unsafe_package"
    write_markdown_normalization(normalized, package)
    document, structure, manifest = _read_package(package)
    overlay = _complete_overlay(document, structure, manifest)

    with pytest.raises(SourcePackageExportError, match="unsafe source markup"):
        export_source_package(package, overlay, tmp_path / "export")


def test_cli_exports_the_same_manifest_contract(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    overlay_path = tmp_path / "overlay.json"
    overlay_path.write_text(
        json.dumps(overlay, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "cli_export"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pipeline.scripts.export_source_package",
            "--package",
            str(package),
            "--translations",
            str(overlay_path),
            "--output-dir",
            str(output),
        ],
        cwd=Path(__file__).parents[2],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    reported = json.loads(completed.stdout)
    stored = json.loads((output / "export_manifest.json").read_text(encoding="utf-8"))
    assert reported == stored


@pytest.mark.skipif(not PANDOC_AVAILABLE, reason="Pandoc is required")
def test_primary_epub_fixture_reconstructs_offline(tmp_path: Path) -> None:
    normalized = normalize_epub(
        RICH_FIXTURE_ROOT / "source.epub",
        doc_id="p2_epub_fixture",
    )
    package = tmp_path / "epub_package"
    write_epub_normalization(normalized, package)
    document, structure, manifest = _read_package(package)
    overlay = _complete_overlay(document, structure, manifest)

    result = export_source_package(package, overlay, tmp_path / "epub_export")

    assert result.manifest["counts"]["chapters_rendered"] == 3
    assert result.manifest["counts"]["chapters_excluded"] == 1
    assert result.manifest["counts"]["assets_copied"] == len(
        [asset for asset in manifest["assets"] if asset["availability"] == "materialized"]
    )
    html_text = result.html_path.read_text(encoding="utf-8")
    assert 'data-chapter-id="p2_epub_fixture_u0003_chapter_ii_the_river_crossing"' in html_text
    assert "VI::p2_epub_fixture_u0003_chapter_ii_the_river_crossing_b0001" in html_text


def test_existing_output_directory_is_not_overwritten(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)
    output = tmp_path / "export"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(SourcePackageExportError, match="already exists"):
        export_source_package(package, overlay, output)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_output_cannot_be_written_inside_the_source_package(tmp_path: Path) -> None:
    package, document, structure, manifest = _html_package(tmp_path)
    overlay = _complete_overlay(document, structure, manifest)

    with pytest.raises(SourcePackageExportError, match="must not live inside"):
        export_source_package(package, overlay, package / "export")

    assert not (package / "export").exists()
