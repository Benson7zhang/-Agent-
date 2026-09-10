"""OCR engine boundary and immutable page evidence contracts."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from threading import local
from typing import Any, Protocol, Sequence

from .config import OCRConfig

OCR_PREPROCESSING_VERSION = "pdf-raster-v1"


class OCRError(RuntimeError):
    """Base error for an explicitly configured OCR operation."""


class OCRDependencyError(OCRError):
    """The configured OCR runtime is unavailable."""


class OCRPageError(OCRError):
    """A physical PDF page could not be rasterized or recognized."""


@dataclass(frozen=True, slots=True)
class OCRToken:
    token_id: str
    page_no: int
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]
    page_image_sha256: str

    def __post_init__(self) -> None:
        if not self.token_id or self.page_no < 1 or not self.text.strip():
            raise ValueError("OCR token requires an ID, a positive page number, and non-empty text")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("OCR token confidence must be finite and between 0 and 1")
        if len(self.bbox) != 4 or any(not math.isfinite(value) or not 0 <= value <= 1 for value in self.bbox):
            raise ValueError("OCR token bbox must contain four normalized finite coordinates")
        left, top, right, bottom = self.bbox
        if left >= right or top >= bottom:
            raise ValueError("OCR token bbox must contain normalized ordered coordinates")
        if not _is_sha256(self.page_image_sha256):
            raise ValueError("OCR token page image hash must be a lowercase SHA-256")

    def to_dict(self) -> dict[str, Any]:
        left, top, right, bottom = self.bbox
        return {
            "token_id": self.token_id,
            "region_id": self.token_id,
            "page_no": self.page_no,
            "text": self.text,
            "confidence": self.confidence,
            "bbox": {
                "x": left,
                "y": top,
                "width": round(right - left, 6),
                "height": round(bottom - top, 6),
                "coordinate_space": "normalized_top_left",
            },
            "page_image_sha256": self.page_image_sha256,
            "source_type": "ocr",
        }


@dataclass(frozen=True, slots=True)
class OCRPage:
    page_no: int
    text: str
    tokens: tuple[OCRToken, ...]
    page_image_sha256: str

    def __post_init__(self) -> None:
        if self.page_no < 1:
            raise ValueError("OCR page number must be positive")
        if not _is_sha256(self.page_image_sha256):
            raise ValueError("OCR page image hash must be a lowercase SHA-256")
        if any(token.page_no != self.page_no for token in self.tokens):
            raise ValueError("OCR page tokens must belong to the same page")
        if any(token.page_image_sha256 != self.page_image_sha256 for token in self.tokens):
            raise ValueError("OCR page tokens must reference the same page image")
        token_ids = [token.token_id for token in self.tokens]
        if len(token_ids) != len(set(token_ids)):
            raise ValueError("OCR page token IDs must be unique")


class OCREngine(Protocol):
    name: str
    version: str

    def recognize_page(self, pdf_path: Path, page_no: int, *, dpi: int) -> OCRPage: ...


class RapidOCREngine:
    """CPU OCR backed by bundled PP-OCR models and ONNX Runtime."""

    name = "rapidocr"

    def __init__(self, config: OCRConfig) -> None:
        if config.engine != self.name:
            raise ValueError(f"RapidOCREngine cannot serve OCR engine {config.engine!r}")
        try:
            self._rapidocr = import_module("rapidocr")
            self._pdfium = import_module("pypdfium2")
        except ImportError as exc:
            raise OCRDependencyError(
                'OCR_ENGINE_UNAVAILABLE: install the OCR dependencies with python -m pip install -e ".[ocr]"'
            ) from exc
        try:
            package_version = version("rapidocr")
        except PackageNotFoundError:  # pragma: no cover - importable packages have distribution metadata
            package_version = "unknown"
        self.version = f"rapidocr-{package_version}/PP-OCRv6"
        self.config = config
        self._thread_state = local()

    def recognize_page(self, pdf_path: Path, page_no: int, *, dpi: int) -> OCRPage:
        if page_no < 1:
            raise ValueError("page_no must be positive")
        document = None
        page = None
        bitmap = None
        try:
            document = self._pdfium.PdfDocument(str(pdf_path))
            if page_no > len(document):
                raise OCRPageError(f"OCR_PAGE_FAILED: page {page_no} is outside the PDF")
            page = document[page_no - 1]
            width, height = page.get_size()
            raster_width, raster_height = _raster_dimensions(width, height, dpi)
            if raster_width * raster_height > self.config.max_page_pixels:
                raise OCRPageError(
                    "OCR_PAGE_TOO_LARGE: "
                    f"page {page_no} would render to {raster_width}x{raster_height} pixels, "
                    f"above OCR_MAX_PAGE_PIXELS={self.config.max_page_pixels}"
                )
            bitmap = page.render(scale=dpi / 72)
            image = bitmap.to_pil().convert("RGB")
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            image_bytes = buffer.getvalue()
            output = self._client()(image_bytes, text_score=float(self.config.min_confidence))
            return self.page_from_output(
                page_no=page_no,
                output=output,
                image_width=image.width,
                image_height=image.height,
                page_image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            )
        except OCRError:
            raise
        except Exception as exc:
            raise OCRPageError(f"OCR_PAGE_FAILED: failed to recognize PDF page {page_no}") from exc
        finally:
            for resource in (bitmap, page, document):
                close = getattr(resource, "close", None)
                if callable(close):
                    close()

    def _client(self) -> Any:
        client = getattr(self._thread_state, "client", None)
        if client is None:
            try:
                client = self._rapidocr.RapidOCR(params={"Global.log_level": "error"})
            except Exception as exc:
                raise OCRDependencyError("OCR_MODEL_LOAD_FAILED: RapidOCR models could not be loaded") from exc
            self._thread_state.client = client
        return client

    @staticmethod
    def page_from_output(
        *,
        page_no: int,
        output: Any,
        image_width: int,
        image_height: int,
        page_image_sha256: str,
    ) -> OCRPage:
        if image_width < 1 or image_height < 1:
            raise ValueError("OCR image dimensions must be positive")
        boxes = getattr(output, "boxes", None)
        texts = getattr(output, "txts", None)
        scores = getattr(output, "scores", None)
        if boxes is None or texts is None or scores is None:
            return OCRPage(page_no, "", (), page_image_sha256)
        if not (len(boxes) == len(texts) == len(scores)):
            raise OCRPageError("OCR_PAGE_FAILED: OCR output arrays have different lengths")

        raw_items: list[tuple[float, float, float, float, str, float]] = []
        for box, raw_text, raw_score in zip(boxes, texts, scores, strict=True):
            text = str(raw_text).strip()
            score = float(raw_score)
            points = list(box)
            if not text or len(points) < 4 or not math.isfinite(score) or not 0 <= score <= 1:
                continue
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            if any(not math.isfinite(value) for value in (*xs, *ys)):
                raise OCRPageError("OCR_PAGE_FAILED: detector returned non-finite coordinates")
            left = max(0.0, min(xs)) / image_width
            top = max(0.0, min(ys)) / image_height
            right = min(float(image_width), max(xs)) / image_width
            bottom = min(float(image_height), max(ys)) / image_height
            if left >= right or top >= bottom:
                continue
            raw_items.append((left, top, right, bottom, text, score))

        ordered_rows = _reading_order(raw_items)
        tokens: list[OCRToken] = []
        text_rows: list[str] = []
        for row in ordered_rows:
            row_tokens: list[str] = []
            for item in row:
                left, top, right, bottom, text, score = item
                token = OCRToken(
                    token_id=f"ocr-p{page_no}-{len(tokens) + 1:04d}",
                    page_no=page_no,
                    text=text,
                    confidence=score,
                    bbox=tuple(round(value, 6) for value in (left, top, right, bottom)),
                    page_image_sha256=page_image_sha256,
                )
                tokens.append(token)
                row_tokens.append(text)
            if row_tokens:
                text_rows.append(" ".join(row_tokens))
        return OCRPage(page_no, "\n".join(text_rows), tuple(tokens), page_image_sha256)


def _reading_order(
    items: Sequence[tuple[float, float, float, float, str, float]],
) -> list[list[tuple[float, float, float, float, str, float]]]:
    rows: list[list[tuple[float, float, float, float, str, float]]] = []
    for item in sorted(items, key=lambda value: ((value[1] + value[3]) / 2, value[0])):
        center_y = (item[1] + item[3]) / 2
        if rows:
            previous = rows[-1]
            previous_center = sum((value[1] + value[3]) / 2 for value in previous) / len(previous)
            tolerance = max(item[3] - item[1], max(value[3] - value[1] for value in previous)) * 0.5
            if abs(center_y - previous_center) <= tolerance:
                previous.append(item)
                previous.sort(key=lambda value: value[0])
                continue
        rows.append([item])
    return rows


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _raster_dimensions(width_points: float, height_points: float, dpi: int) -> tuple[int, int]:
    dimensions = (float(width_points), float(height_points))
    if any(not math.isfinite(value) or value <= 0 for value in dimensions):
        raise OCRPageError("OCR_PAGE_FAILED: PDF page dimensions must be positive and finite")
    scale = dpi / 72
    return math.ceil(dimensions[0] * scale), math.ceil(dimensions[1] * scale)
