import sys
from pathlib import Path

import pytest

from adrenalin.common import ocr
from adrenalin.common.ocr import OcrUnavailable, recognize

FIX = Path(__file__).parent.parent / "fixtures" / "ads_transparency"


def test_recognize_reads_text_ad_fixture():
    try:
        r = recognize(FIX / "simgad_text.png")
    except OcrUnavailable:
        pytest.skip("no OCR engine")
    assert "Book Cheap Flight Tickets" in r.text
    assert "flights70.com" in r.text
    assert r.confidence > 0.6
    assert r.lines == sorted(r.lines, key=lambda l: l.box[1])


def test_recognize_without_engines_raises_ocr_unavailable(monkeypatch):
    from adrenalin.common.ocr import apple_vision, tesseract

    def boom(path):
        raise ImportError("missing")

    monkeypatch.setattr(apple_vision, "recognize", boom)
    monkeypatch.setattr(tesseract, "recognize", boom)
    with pytest.raises(OcrUnavailable, match="tesseract"):
        recognize(FIX / "simgad_text.png")


def test_ocr_result_properties():
    from adrenalin.common.ocr import OcrLine, OcrResult

    assert OcrResult([], "x").confidence == 0.0
    r = OcrResult([OcrLine("a", 0.5, (0, 0, 1, 1)), OcrLine("b", 1.0, (0, 0.5, 1, 1))], "x")
    assert r.text == "a\nb" and r.confidence == 0.75
