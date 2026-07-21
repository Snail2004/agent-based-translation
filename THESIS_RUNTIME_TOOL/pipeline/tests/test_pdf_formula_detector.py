from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy
import pytest

from pipeline.ingest.pdf_formula_detector import (
    MODEL_FILENAME,
    MODEL_LABELS,
    MODEL_SHA256,
    FormulaDetectorConfig,
    PdfFormulaDetectorError,
    detect_pdf_formula_regions,
)


def _write_pdf(path: Path) -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 72), "Formula detector fixture")
    document.save(path)
    document.close()
    return path


def _config(model: Path, **overrides) -> FormulaDetectorConfig:
    values = {
        "model_path": model,
    }
    values.update(overrides)
    return FormulaDetectorConfig(**values)


def _pinned_model_hash(_model_path: Path) -> str:
    return MODEL_SHA256


class _FakeSession:
    def __init__(
        self,
        *,
        names: dict[int, str] | None = None,
        output: numpy.ndarray | None = None,
    ) -> None:
        self.inputs: list[numpy.ndarray] = []
        self._names = MODEL_LABELS if names is None else names
        self._output = (
            output
            if output is not None
            else numpy.array(
                [
                    [
                        [180.0, 240.0, 840.0, 330.0, 0.91, 8.0],
                        [182.0, 242.0, 838.0, 328.0, 0.80, 8.0],
                        [0.0, 0.0, 10.0, 10.0, 0.99, 3.0],
                    ]
                ],
                dtype=numpy.float32,
            )
        )

    def get_inputs(self):
        return [
            SimpleNamespace(
                name="images",
                shape=["batch", 3, "height", "width"],
                type="tensor(float)",
            )
        ]

    def get_outputs(self):
        return [
            SimpleNamespace(
                name="output0",
                shape=["batch", "detections", 6],
                type="tensor(float)",
            )
        ]

    def get_modelmeta(self):
        return SimpleNamespace(
            custom_metadata_map={
                "names": repr(self._names),
                "imgsz": "[1024, 1024]",
                "task": "detect",
                "license": "AGPL-3.0 License",
            }
        )

    def run(self, output_names, inputs):
        assert output_names == ["output0"]
        tensor = inputs["images"]
        self.inputs.append(tensor.copy())
        return [self._output.copy()]


def test_detector_scans_full_page_and_is_deterministic(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    model = tmp_path / MODEL_FILENAME
    model.write_bytes(b"fixture-onnx")
    config = _config(model)
    sessions: list[_FakeSession] = []

    def factory(_model_path: Path, provider: str):
        assert provider == "CPUExecutionProvider"
        session = _FakeSession()
        sessions.append(session)
        return session, "fixture-onnxruntime"

    first = detect_pdf_formula_regions(
        source,
        config,
        session_factory=factory,
        file_hasher=_pinned_model_hash,
    )
    second = detect_pdf_formula_regions(
        source,
        config,
        session_factory=factory,
        file_hasher=_pinned_model_hash,
    )

    assert first == second
    assert len(first.regions) == 1
    assert first.regions[0].label == "isolate_formula"
    assert first.regions[0].confidence == pytest.approx(0.91)
    assert first.detector_manifest["model"]["sha256"] == (
        config.expected_model_sha256
    )
    assert first.detector_manifest["preprocessing"] == {
        "raster_dpi": 144,
        "color_mode": "RGB",
        "input_size": 1024,
        "normalization": "uint8_rgb_div_255",
        "resize": "aspect_fit_bilinear",
        "letterbox_fill": 114,
        "coordinate_transform": "raster_top_left_to_pdf_bottom_left",
    }
    assert sessions[0].inputs[0].shape == (1, 3, 1024, 1024)
    assert sessions[0].inputs[0].dtype == numpy.float32
    assert 0.0 <= float(sessions[0].inputs[0].min())
    assert float(sessions[0].inputs[0].max()) <= 1.0
    region = first.regions[0]
    left, top, width, height = region.bbox_view
    reconstructed = (
        left * 612.0,
        792.0 - ((top + height) * 792.0),
        (left + width) * 612.0,
        792.0 - (top * 792.0),
    )
    assert reconstructed == pytest.approx(region.bbox_pdf, abs=0.002)


def test_detector_fails_closed_on_model_label_and_output_drift(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    model = tmp_path / MODEL_FILENAME
    model.write_bytes(b"fixture-onnx")

    missing = FormulaDetectorConfig(model_path=tmp_path / "missing" / MODEL_FILENAME)
    with pytest.raises(PdfFormulaDetectorError, match="unavailable"):
        detect_pdf_formula_regions(source, missing)

    with pytest.raises(PdfFormulaDetectorError, match="sealed v1 contract"):
        FormulaDetectorConfig(
            model_path=model,
            model_repo="untrusted/model-repository",
        )
    with pytest.raises(PdfFormulaDetectorError, match="sealed filename"):
        FormulaDetectorConfig(model_path=tmp_path / "renamed-model.onnx")

    with pytest.raises(PdfFormulaDetectorError, match="SHA-256 mismatch"):
        detect_pdf_formula_regions(
            source,
            _config(model),
            file_hasher=lambda _model_path: "0" * 64,
        )

    def label_drift(_model_path: Path, _provider: str):
        labels = dict(MODEL_LABELS)
        labels[8] = "future_formula"
        return _FakeSession(names=labels), "fixture-runtime"

    with pytest.raises(PdfFormulaDetectorError, match="label map drifted"):
        detect_pdf_formula_regions(
            source,
            _config(model),
            session_factory=label_drift,
            file_hasher=_pinned_model_hash,
        )

    def shape_drift(_model_path: Path, _provider: str):
        return (
            _FakeSession(output=numpy.zeros((1, 6, 300), dtype=numpy.float32)),
            "fixture-runtime",
        )

    with pytest.raises(PdfFormulaDetectorError, match="output shape drifted"):
        detect_pdf_formula_regions(
            source,
            _config(model),
            session_factory=shape_drift,
            file_hasher=_pinned_model_hash,
        )


def test_detector_rejects_nonfinite_and_foreign_class_outputs(
    tmp_path: Path,
) -> None:
    source = _write_pdf(tmp_path / "fixture.pdf")
    model = tmp_path / MODEL_FILENAME
    model.write_bytes(b"fixture-onnx")

    nonfinite = numpy.zeros((1, 1, 6), dtype=numpy.float32)
    nonfinite[0, 0] = [1.0, 1.0, 2.0, 2.0, numpy.nan, 8.0]

    def nonfinite_factory(_model_path: Path, _provider: str):
        return _FakeSession(output=nonfinite), "fixture-runtime"

    with pytest.raises(PdfFormulaDetectorError, match="not finite"):
        detect_pdf_formula_regions(
            source,
            _config(model),
            session_factory=nonfinite_factory,
            file_hasher=_pinned_model_hash,
        )

    foreign = numpy.array(
        [[[1.0, 1.0, 2.0, 2.0, 0.9, 12.0]]],
        dtype=numpy.float32,
    )

    def foreign_factory(_model_path: Path, _provider: str):
        return _FakeSession(output=foreign), "fixture-runtime"

    with pytest.raises(PdfFormulaDetectorError, match="class id drifted"):
        detect_pdf_formula_regions(
            source,
            _config(model),
            session_factory=foreign_factory,
            file_hasher=_pinned_model_hash,
        )
