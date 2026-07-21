from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


DETECTOR_SCHEMA_VERSION = "pdf_formula_detector_v1"
MODEL_REPO = "wybxc/DocLayout-YOLO-DocStructBench-onnx"
MODEL_REVISION = "ee7c3d744e5c47c58e267044ac825f95abe69653"
MODEL_FILENAME = "doclayout_yolo_docstructbench_imgsz1024.onnx"
MODEL_SHA256 = "fece9af02f618b603ff7921ccec6861d13e7e1f9830e091dfb7e8ad9311e5b21"
MODEL_DECLARED_LICENSE = "AGPL-3.0"
MODEL_LABELS = {
    0: "title",
    1: "plain text",
    2: "abandon",
    3: "figure",
    4: "figure_caption",
    5: "table",
    6: "table_caption",
    7: "table_footnote",
    8: "isolate_formula",
    9: "formula_caption",
}
FORMULA_LABELS = {"isolate_formula", "formula_caption"}


class PdfFormulaDetectorError(ValueError):
    pass


@dataclass(frozen=True)
class FormulaDetectorConfig:
    model_path: Path
    expected_model_sha256: str = MODEL_SHA256
    model_repo: str = MODEL_REPO
    model_revision: str = MODEL_REVISION
    model_filename: str = MODEL_FILENAME
    model_declared_license: str = MODEL_DECLARED_LICENSE
    input_size: int = 1024
    raster_dpi: int = 144
    color_mode: str = "RGB"
    confidence_threshold: float = 0.10
    acceptance_threshold: float = 0.25
    nms_iou_threshold: float = 0.45
    provider: str = "CPUExecutionProvider"

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_path", Path(self.model_path).resolve())
        if not all(
            isinstance(value, str)
            for value in (
                self.expected_model_sha256,
                self.model_repo,
                self.model_revision,
                self.model_filename,
                self.model_declared_license,
            )
        ):
            raise PdfFormulaDetectorError(
                "formula detector model identity must contain strings"
            )
        if (
            self.expected_model_sha256.casefold() != MODEL_SHA256
            or self.model_repo != MODEL_REPO
            or self.model_revision != MODEL_REVISION
            or self.model_filename != MODEL_FILENAME
            or self.model_declared_license != MODEL_DECLARED_LICENSE
        ):
            raise PdfFormulaDetectorError(
                "formula detector model identity must match the sealed v1 contract"
            )
        if self.model_path.name != MODEL_FILENAME:
            raise PdfFormulaDetectorError(
                "formula detector model path must use the sealed filename"
            )
        if self.input_size != 1024:
            raise PdfFormulaDetectorError("formula detector input_size must be 1024")
        if self.raster_dpi <= 0:
            raise PdfFormulaDetectorError("formula detector raster_dpi must be positive")
        if self.color_mode != "RGB":
            raise PdfFormulaDetectorError("formula detector color mode must be RGB")
        if not 0.0 <= self.confidence_threshold <= self.acceptance_threshold <= 1.0:
            raise PdfFormulaDetectorError("formula detector confidence thresholds are invalid")
        if not 0.0 < self.nms_iou_threshold < 1.0:
            raise PdfFormulaDetectorError("formula detector NMS threshold is invalid")
        if self.provider != "CPUExecutionProvider":
            raise PdfFormulaDetectorError(
                "formula detector provider must be CPUExecutionProvider in v1"
            )
        expected = self.expected_model_sha256.casefold()
        if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
            raise PdfFormulaDetectorError("formula detector model SHA-256 is invalid")
        object.__setattr__(self, "expected_model_sha256", expected)


@dataclass(frozen=True)
class FormulaRegion:
    region_id: str
    page_number: int
    label: str
    class_id: int
    confidence: float
    bbox_pdf: tuple[float, float, float, float]
    bbox_view: tuple[float, float, float, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "page_number": self.page_number,
            "label": self.label,
            "class_id": self.class_id,
            "confidence": self.confidence,
            "bbox_pdf": list(self.bbox_pdf),
            "bbox_view": {
                "left": self.bbox_view[0],
                "top": self.bbox_view[1],
                "width": self.bbox_view[2],
                "height": self.bbox_view[3],
            },
        }


@dataclass(frozen=True)
class FormulaDetectionResult:
    source_sha256: str
    page_count: int
    regions: tuple[FormulaRegion, ...]
    detector_manifest: dict[str, Any]


SessionFactory = Callable[[Path, str], tuple[Any, str]]
FileHasher = Callable[[Path], str]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_names(value: str) -> dict[int, str]:
    try:
        payload = ast.literal_eval(value)
    except (SyntaxError, ValueError) as exc:
        raise PdfFormulaDetectorError("formula detector label metadata is invalid") from exc
    if not isinstance(payload, dict):
        raise PdfFormulaDetectorError("formula detector label metadata must be a mapping")
    try:
        names = {int(key): str(label) for key, label in payload.items()}
    except (TypeError, ValueError) as exc:
        raise PdfFormulaDetectorError("formula detector label metadata is invalid") from exc
    if names != MODEL_LABELS:
        raise PdfFormulaDetectorError("formula detector label map drifted")
    return names


def _default_session_factory(model_path: Path, provider: str) -> tuple[Any, str]:
    try:
        import onnxruntime
    except ImportError as exc:
        raise PdfFormulaDetectorError(
            "onnxruntime is unavailable for required formula detection"
        ) from exc
    try:
        runtime_version = importlib.metadata.version("onnxruntime")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PdfFormulaDetectorError("onnxruntime version identity is unavailable") from exc
    if provider not in onnxruntime.get_available_providers():
        raise PdfFormulaDetectorError(
            f"required ONNX provider is unavailable: {provider}"
        )
    try:
        session = onnxruntime.InferenceSession(
            str(model_path),
            providers=[provider],
        )
    except Exception as exc:
        raise PdfFormulaDetectorError("formula detector ONNX session failed to load") from exc
    return session, runtime_version


def _validate_session(session: Any, config: FormulaDetectorConfig) -> dict[int, str]:
    inputs = list(session.get_inputs())
    outputs = list(session.get_outputs())
    if len(inputs) != 1 or inputs[0].name != "images":
        raise PdfFormulaDetectorError("formula detector input contract drifted")
    if inputs[0].type != "tensor(float)":
        raise PdfFormulaDetectorError("formula detector input dtype drifted")
    input_shape = list(inputs[0].shape)
    if len(input_shape) != 4 or input_shape[1] != 3:
        raise PdfFormulaDetectorError("formula detector input shape drifted")
    if len(outputs) != 1 or outputs[0].name != "output0":
        raise PdfFormulaDetectorError("formula detector output contract drifted")
    if outputs[0].type != "tensor(float)":
        raise PdfFormulaDetectorError("formula detector output dtype drifted")

    metadata = dict(session.get_modelmeta().custom_metadata_map)
    names = _parse_names(str(metadata.get("names") or ""))
    try:
        declared_size = ast.literal_eval(str(metadata.get("imgsz") or ""))
    except (SyntaxError, ValueError) as exc:
        raise PdfFormulaDetectorError("formula detector imgsz metadata is invalid") from exc
    if declared_size != [config.input_size, config.input_size]:
        raise PdfFormulaDetectorError("formula detector imgsz metadata drifted")
    if str(metadata.get("task") or "") != "detect":
        raise PdfFormulaDetectorError("formula detector task metadata drifted")
    license_value = str(metadata.get("license") or "")
    if config.model_declared_license not in license_value:
        raise PdfFormulaDetectorError("formula detector license metadata drifted")
    return names


def _letterbox(
    image: Any,
    *,
    target_size: int,
) -> tuple[Any, float, int, int]:
    import numpy
    from PIL import Image

    if not isinstance(image, numpy.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        raise PdfFormulaDetectorError("formula detector page raster must be RGB")
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        raise PdfFormulaDetectorError("formula detector page raster is empty")
    scale = min(target_size / float(width), target_size / float(height))
    resized_width = max(1, min(target_size, int(round(width * scale))))
    resized_height = max(1, min(target_size, int(round(height * scale))))
    resized = Image.fromarray(image, mode="RGB").resize(
        (resized_width, resized_height),
        Image.Resampling.BILINEAR,
    )
    pad_x = (target_size - resized_width) // 2
    pad_y = (target_size - resized_height) // 2
    canvas = numpy.full((target_size, target_size, 3), 114, dtype=numpy.uint8)
    canvas[
        pad_y : pad_y + resized_height,
        pad_x : pad_x + resized_width,
    ] = numpy.asarray(resized)
    tensor = numpy.transpose(canvas, (2, 0, 1))[None].astype(numpy.float32) / 255.0
    return tensor, scale, pad_x, pad_y


def _intersection_over_union(left: list[float], right: list[float]) -> float:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if intersection <= 0.0:
        return 0.0
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


def _classwise_nms(rows: list[list[float]], *, threshold: float) -> list[list[float]]:
    kept: list[list[float]] = []
    for class_id in sorted({int(round(row[5])) for row in rows}):
        class_rows = [row for row in rows if int(round(row[5])) == class_id]
        class_rows.sort(
            key=lambda row: (
                -row[4],
                row[1],
                row[0],
                row[3],
                row[2],
            )
        )
        while class_rows:
            winner = class_rows.pop(0)
            kept.append(winner)
            class_rows = [
                candidate
                for candidate in class_rows
                if _intersection_over_union(winner, candidate) <= threshold
            ]
    kept.sort(key=lambda row: (row[1], row[0], int(round(row[5])), -row[4]))
    return kept


def _decode_rows(
    output: Any,
    *,
    config: FormulaDetectorConfig,
    names: Mapping[int, str],
    scale: float,
    pad_x: int,
    pad_y: int,
    raster_width: int,
    raster_height: int,
) -> list[list[float]]:
    import numpy

    rows = numpy.asarray(output)
    if rows.ndim != 3 or rows.shape[0] != 1 or rows.shape[2] != 6:
        raise PdfFormulaDetectorError("formula detector runtime output shape drifted")
    if not numpy.isfinite(rows).all():
        raise PdfFormulaDetectorError("formula detector runtime output is not finite")
    decoded: list[list[float]] = []
    for raw in rows[0]:
        x0, y0, x1, y1, confidence, raw_class_id = [
            float(value) for value in raw
        ]
        class_id = int(round(raw_class_id))
        if abs(raw_class_id - class_id) > 1e-4 or class_id not in names:
            raise PdfFormulaDetectorError("formula detector runtime class id drifted")
        if names[class_id] not in FORMULA_LABELS:
            continue
        if confidence < config.confidence_threshold:
            continue
        x0 = max(0.0, min(float(raster_width), (x0 - pad_x) / max(scale, 1e-9)))
        x1 = max(0.0, min(float(raster_width), (x1 - pad_x) / max(scale, 1e-9)))
        y0 = max(0.0, min(float(raster_height), (y0 - pad_y) / max(scale, 1e-9)))
        y1 = max(0.0, min(float(raster_height), (y1 - pad_y) / max(scale, 1e-9)))
        if x1 <= x0 or y1 <= y0:
            continue
        decoded.append([x0, y0, x1, y1, confidence, float(class_id)])
    return _classwise_nms(decoded, threshold=config.nms_iou_threshold)


def formula_region_id(
    *,
    page_number: int,
    label: str,
    bbox_pdf: tuple[float, float, float, float],
) -> str:
    payload = (
        f"{page_number}|{label}|"
        + "|".join(f"{coordinate:.3f}" for coordinate in bbox_pdf)
    )
    return "freg_" + hashlib.sha256(payload.encode("ascii")).hexdigest()[:20]


def detect_pdf_formula_regions(
    source_path: str | Path,
    config: FormulaDetectorConfig,
    *,
    session_factory: SessionFactory | None = None,
    file_hasher: FileHasher = _sha256_file,
) -> FormulaDetectionResult:
    source = Path(source_path).resolve()
    if source.suffix.casefold() != ".pdf" or not source.is_file():
        raise PdfFormulaDetectorError("formula detector requires an existing PDF")
    if not config.model_path.is_file():
        raise PdfFormulaDetectorError(
            f"required formula detector model is unavailable: {config.model_path}"
        )
    actual_model_sha256 = file_hasher(config.model_path).casefold()
    if actual_model_sha256 != config.expected_model_sha256.casefold():
        raise PdfFormulaDetectorError("formula detector model SHA-256 mismatch")

    session, runtime_version = (session_factory or _default_session_factory)(
        config.model_path,
        config.provider,
    )
    names = _validate_session(session, config)
    try:
        import pymupdf
    except ImportError as exc:
        raise PdfFormulaDetectorError(
            "PyMuPDF is unavailable for formula detector page rasterization"
        ) from exc
    try:
        pymupdf_version = importlib.metadata.version("PyMuPDF")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PdfFormulaDetectorError("PyMuPDF version identity is unavailable") from exc

    regions: list[FormulaRegion] = []
    document = pymupdf.open(source)
    page_count = len(document)
    try:
        for page_index, page in enumerate(document):
            matrix = pymupdf.Matrix(config.raster_dpi / 72.0, config.raster_dpi / 72.0)
            pixmap = page.get_pixmap(
                matrix=matrix,
                colorspace=pymupdf.csRGB,
                alpha=False,
            )
            import numpy

            raster = numpy.frombuffer(pixmap.samples, dtype=numpy.uint8).reshape(
                pixmap.height,
                pixmap.width,
                3,
            )
            tensor, scale, pad_x, pad_y = _letterbox(
                raster,
                target_size=config.input_size,
            )
            try:
                output = session.run(["output0"], {"images": tensor})[0]
            except Exception as exc:
                raise PdfFormulaDetectorError(
                    f"formula detector inference failed on page {page_index + 1}"
                ) from exc
            decoded = _decode_rows(
                output,
                config=config,
                names=names,
                scale=scale,
                pad_x=pad_x,
                pad_y=pad_y,
                raster_width=pixmap.width,
                raster_height=pixmap.height,
            )
            page_width = float(page.rect.width)
            page_height = float(page.rect.height)
            for x0, y0, x1, y1, confidence, class_value in decoded:
                class_id = int(round(class_value))
                pdf_x0 = x0 * page_width / pixmap.width
                pdf_x1 = x1 * page_width / pixmap.width
                pdf_top = y0 * page_height / pixmap.height
                pdf_bottom = y1 * page_height / pixmap.height
                bbox_pdf = (
                    round(pdf_x0, 3),
                    round(page_height - pdf_bottom, 3),
                    round(pdf_x1, 3),
                    round(page_height - pdf_top, 3),
                )
                bbox_view = (
                    round(x0 / pixmap.width, 6),
                    round(y0 / pixmap.height, 6),
                    round((x1 - x0) / pixmap.width, 6),
                    round((y1 - y0) / pixmap.height, 6),
                )
                label = names[class_id]
                regions.append(
                    FormulaRegion(
                        region_id=formula_region_id(
                            page_number=page_index + 1,
                            label=label,
                            bbox_pdf=bbox_pdf,
                        ),
                        page_number=page_index + 1,
                        label=label,
                        class_id=class_id,
                        confidence=round(confidence, 6),
                        bbox_pdf=bbox_pdf,
                        bbox_view=bbox_view,
                    )
                )
    finally:
        document.close()

    regions.sort(
        key=lambda region: (
            region.page_number,
            -region.bbox_pdf[3],
            region.bbox_pdf[0],
            region.label,
            region.region_id,
        )
    )
    source_sha256 = _sha256_file(source)
    manifest = {
        "schema_version": DETECTOR_SCHEMA_VERSION,
        "mode": "required",
        "status": "completed",
        "source_sha256": source_sha256,
        "model": {
            "repo": config.model_repo,
            "revision": config.model_revision,
            "filename": config.model_filename,
            "sha256": actual_model_sha256,
            "declared_license": config.model_declared_license,
            "redistribution_reviewed": False,
        },
        "runtime": {
            "onnxruntime_version": runtime_version,
            "provider": config.provider,
            "pymupdf_version": pymupdf_version,
        },
        "preprocessing": {
            "raster_dpi": config.raster_dpi,
            "color_mode": config.color_mode,
            "input_size": config.input_size,
            "normalization": "uint8_rgb_div_255",
            "resize": "aspect_fit_bilinear",
            "letterbox_fill": 114,
            "coordinate_transform": "raster_top_left_to_pdf_bottom_left",
        },
        "postprocessing": {
            "label_map": {str(key): value for key, value in MODEL_LABELS.items()},
            "included_labels": sorted(FORMULA_LABELS),
            "confidence_threshold": config.confidence_threshold,
            "acceptance_threshold": config.acceptance_threshold,
            "nms_iou_threshold": config.nms_iou_threshold,
            "nms_mode": "classwise_deterministic",
        },
        "page_count": page_count,
        "region_count": len(regions),
        "regions": [region.as_dict() for region in regions],
    }
    return FormulaDetectionResult(
        source_sha256=source_sha256,
        page_count=page_count,
        regions=tuple(regions),
        detector_manifest=manifest,
    )


__all__ = [
    "DETECTOR_SCHEMA_VERSION",
    "FORMULA_LABELS",
    "MODEL_DECLARED_LICENSE",
    "MODEL_FILENAME",
    "MODEL_LABELS",
    "MODEL_REPO",
    "MODEL_REVISION",
    "MODEL_SHA256",
    "FormulaDetectionResult",
    "FormulaDetectorConfig",
    "FormulaRegion",
    "PdfFormulaDetectorError",
    "detect_pdf_formula_regions",
    "formula_region_id",
]
