from __future__ import annotations

from pathlib import Path

from .base import OcrLine, OcrResult


def recognize(path: Path) -> OcrResult:
    from ocrmac import ocrmac  # lazy: macOS only

    raw = ocrmac.OCR(str(path), recognition_level="accurate").recognize()
    lines = []
    for text, conf, (x, y, w, h) in raw:
        # Vision boxes use a bottom-left origin; convert to top-left.
        lines.append(OcrLine(text, float(conf), (x, 1.0 - y - h, w, h)))
    lines.sort(key=lambda l: l.box[1])
    return OcrResult(lines, "apple_vision")
