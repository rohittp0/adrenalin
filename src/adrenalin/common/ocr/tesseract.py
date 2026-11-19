from __future__ import annotations

from pathlib import Path

from .base import OcrLine, OcrResult


def recognize(path: Path) -> OcrResult:
    import pytesseract
    from PIL import Image

    with Image.open(path) as img:
        img.load()
        width, height = img.size
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    groups: dict[tuple[int, int, int], list[int]] = {}
    for i, word in enumerate(data["text"]):
        if not str(word).strip():
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        groups.setdefault(key, []).append(i)

    lines = []
    for idxs in groups.values():
        x0 = min(data["left"][i] for i in idxs)
        y0 = min(data["top"][i] for i in idxs)
        x1 = max(data["left"][i] + data["width"][i] for i in idxs)
        y1 = max(data["top"][i] + data["height"][i] for i in idxs)
        text = " ".join(str(data["text"][i]).strip() for i in idxs)
        conf = sum(float(data["conf"][i]) for i in idxs) / len(idxs) / 100.0
        lines.append(OcrLine(text, conf, (x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height)))
    lines.sort(key=lambda l: l.box[1])
    return OcrResult(lines, "tesseract")
