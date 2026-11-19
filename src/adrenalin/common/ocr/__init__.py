from __future__ import annotations

import sys
from pathlib import Path

from . import apple_vision, tesseract
from .base import OcrLine, OcrResult, OcrUnavailable

__all__ = ["OcrLine", "OcrResult", "OcrUnavailable", "recognize"]

_NO_ENGINE = "No OCR engine: install tesseract (brew install tesseract / apt install tesseract-ocr)"


def recognize(path: Path) -> OcrResult:
    """Apple Vision on macOS when ocrmac imports, otherwise Tesseract."""
    engines = []
    if sys.platform == "darwin":
        engines.append(apple_vision)
    engines.append(tesseract)
    for engine in engines:
        try:
            return engine.recognize(Path(path))
        except (ImportError, FileNotFoundError, tesseract.TesseractNotFound):
            continue  # engine unavailable: try the next one; other errors propagate
    raise OcrUnavailable(_NO_ENGINE)
