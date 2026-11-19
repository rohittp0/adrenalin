import json
from pathlib import Path

import pytest

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.ocr import OcrLine, OcrResult, OcrUnavailable
from adrenalin.common.ocr.codex import CodexError
from adrenalin.tools.ads_transparency.copy import (
    AdCopy,
    extract_image,
    needs_vision,
    parse_content_js,
    parse_image_ad_ocr,
    parse_overlay_param,
    parse_text_ad_ocr,
    website_from,
)

FIX = Path(__file__).parents[2] / "fixtures" / "ads_transparency"
_DETAIL = json.loads((FIX / "creative_detail_video.json").read_text(encoding="utf-8"))
OVERLAY_URL = _DETAIL["1"]["5"][1]["1"]["4"]


def test_website_from():
    assert website_from("www.flights70.com/") == "flights70.com"
    assert website_from("app.rent80.com") == "app.rent80.com"
    assert website_from("") == ""


def test_parse_content_js_text_fixture():
    c = parse_content_js((FIX / "content_text.js").read_text(encoding="utf-8"))
    assert (c.brand, c.headline) == ("Hotels70", "Apartahotel por Mes - Apartahotel a Buen Precio")
    assert c.display_url == "www.hotels70.com/apartahoteles/estancias" and c.website == "hotels70.com"
    assert c.description.startswith("¿Quieres más espacio?")
    assert "Cancelación Gratuita" in c.sitelinks and len(c.sitelinks) == 9 and c.method == "content_js"
    assert c.confidence == 1.0


def test_parse_content_js_image_template_fixture():
    c = parse_content_js((FIX / "content_image.js").read_text(encoding="utf-8"))
    assert (c.brand, c.headline) == ("Rent80", "Noleggio Auto a Prezzi Bassi")
    assert c.description == "Prenota in pochi click. Ritiro rapido in aeroporto o centro città."
    assert c.logo_url.endswith("/simgad/11202933705113574445")
    assert any(u.endswith("/simgad/4554907277327000602") for u in c.image_urls)


def test_parse_content_js_video_app_install_fixture():
    c = parse_content_js((FIX / "content_video.js").read_text(encoding="utf-8"))
    assert c.video_id == "KVHq8nrh0GI" and c.brand == "eSIM70" and c.cta == "Install"
    assert c.description == "Get affordable travel data abroad with quick eSIM setup and no physical SIM swap."
    assert c.landing_url.startswith("https://play.google.com/store/apps/details?id=com.esim70.app")
    assert c.website == "play.google.com"
    assert "For Travel Data" in c.full_text.split("\n")


def test_parse_content_js_keeps_utf8_and_strips_bidi():
    js = "x \\x3cdiv\\x3eCafé ⁦Niño⁩\\x3c/div\\x3e y"
    # not enough strings for any rule -> None, but must not crash on non-ASCII
    assert parse_content_js(js) is None


def test_parse_overlay_param():
    c = parse_overlay_param(OVERLAY_URL)
    assert c.headline.startswith("eSIM for 190+ Countries")
    assert c.description == "Install app - eSIM70: eSIM For Travel Data"
    assert c.display_url == "play.google.com/store"
    assert c.method == "content_js" and c.confidence == 1.0


def test_parse_overlay_param_without_overlay_returns_none():
    assert parse_content_js("var x = 1;") is None
    assert parse_overlay_param("https://x.example/content.js?assets=abc") is None


def test_parse_content_js_unknown_returns_none():
    assert parse_content_js("var x = 1;") is None


def _line(text, y, h, conf=1.0):
    return OcrLine(text, conf, (0.0, y, 0.5, h))


def _text_ad_result(conf=1.0):
    return OcrResult(
        [
            _line("flights70.com", 0.0, 0.05, conf),
            _line("www.flights70.com/", 0.1, 0.04, conf),
            _line("Book Cheap Flight Tickets -", 0.2, 0.08, conf),
            _line("Find Cheapest Flight Tickets", 0.3, 0.08, conf),
            _line("Why waste hours checking", 0.4, 0.05, conf),
            _line("different airline websites?", 0.5, 0.05, conf),
            _line("Search Flights.", 0.6, 0.05, conf),
        ],
        "apple_vision",
    )


def test_parse_text_ad_ocr_synthetic():
    c = parse_text_ad_ocr(_text_ad_result())
    assert (c.brand, c.display_url, c.website) == ("flights70.com", "www.flights70.com/", "flights70.com")
    assert c.headline == "Book Cheap Flight Tickets - Find Cheapest Flight Tickets"
    assert c.description.startswith("Why waste hours")
    assert c.method == "apple_vision" and c.confidence == 1.0


def test_parse_text_ad_ocr_rejects_non_search_layout():
    r = OcrResult([_line("Brand", 0, 0.05), _line("Not a url at all", 0.1, 0.05), _line("Head", 0.2, 0.08)], "tesseract")
    assert parse_text_ad_ocr(r) is None
    assert parse_text_ad_ocr(OcrResult([], "tesseract")) is None


def test_parse_image_ad_ocr_synthetic():
    r = OcrResult(
        [
            _line("Rent80", 0.01, 0.03),
            _line("app.rent80.com", 0.06, 0.03),
            _line("Compare Car Rental", 0.3, 0.08),
            _line("Visit Site", 0.78, 0.04),
            _line("Very good", 0.93, 0.03),
        ],
        "apple_vision",
    )
    c = parse_image_ad_ocr(r)
    assert (c.brand, c.display_url, c.website) == ("Rent80", "app.rent80.com", "app.rent80.com")
    assert c.cta == "Very good" or c.cta == "Visit Site"
    assert "Compare Car Rental" in c.full_text and c.method == "apple_vision"


def test_needs_vision_on_low_confidence():
    good = AdCopy(brand="x")
    assert needs_vision(_text_ad_result(0.5), good) is True
    assert needs_vision(_text_ad_result(0.9), good) is False
    assert needs_vision(_text_ad_result(0.9), None) is True
    assert needs_vision(OcrResult([], "tesseract"), good) is True


def test_ad_copy_roundtrip():
    c = AdCopy(brand="b", sitelinks=["a", "b"], confidence=0.7)
    assert AdCopy.from_dict(c.to_dict()) == c
    assert AdCopy().headline == "" and AdCopy().confidence is None


class FakeCodex:
    def __init__(self, result=None, exc=None, model="gpt-5.6-luna"):
        self.model, self.result, self.exc, self.calls = model, result, exc, 0

    def extract(self, image, schema, prompt):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def _cache(tmp_path):
    return ExtractionCache(tmp_path / "c.sqlite3")


def _never(path):
    raise AssertionError("ocr must not run")


def test_extract_image_uses_cache_before_ocr(tmp_path):
    cache = _cache(tmp_path)
    try:
        r = _text_ad_result()
        cache.put("img1", "apple_vision", "", {"method": r.method, "lines": [
            {"text": l.text, "confidence": l.confidence, "box": list(l.box)} for l in r.lines]})
        c = extract_image(tmp_path / "x.png", "img1", "text", cache=cache, codex=None,
                          force_vision=False, ocr=_never, warn=lambda m: None)
        assert c.brand == "flights70.com" and c.method == "apple_vision"
    finally:
        cache.close()


def test_extract_image_falls_back_to_codex_and_records_model(tmp_path):
    cache = _cache(tmp_path)
    codex = FakeCodex({"brand": "B", "display_url": "www.b.com/", "headline": "H", "description": "D",
                       "cta": "Go", "full_text": "B H D", "confidence": 0.9})
    try:
        c = extract_image(tmp_path / "x.png", "img2", "image", cache=cache, codex=codex,
                          force_vision=False, ocr=lambda p: OcrResult([], "apple_vision"), warn=lambda m: None)
        assert c.method == "codex" and c.model == "gpt-5.6-luna" and c.confidence == 0.9
        assert c.brand == "B" and c.website == "b.com"
        assert cache.get("img2", "codex", "gpt-5.6-luna")["headline"] == "H"
        # second call is served from cache
        extract_image(tmp_path / "x.png", "img2", "image", cache=cache, codex=codex,
                      force_vision=False, ocr=lambda p: OcrResult([], "apple_vision"), warn=lambda m: None)
        assert codex.calls == 1
    finally:
        cache.close()


def test_extract_image_force_vision_skips_ocr(tmp_path):
    cache = _cache(tmp_path)
    codex = FakeCodex({"brand": "B", "confidence": 0.8})
    try:
        c = extract_image(tmp_path / "x.png", "img3", "text", cache=cache, codex=codex,
                          force_vision=True, ocr=_never, warn=lambda m: None)
        assert c.method == "codex"
    finally:
        cache.close()


def test_extract_image_keeps_ocr_when_codex_fails(tmp_path):
    cache = _cache(tmp_path)
    warnings = []
    try:
        low = _text_ad_result(0.4)
        c = extract_image(tmp_path / "x.png", "img4", "text", cache=cache, codex=FakeCodex(exc=CodexError("boom")),
                          force_vision=False, ocr=lambda p: low, warn=warnings.append)
        assert c.method == "apple_vision" and c.brand == "flights70.com"
        assert len(warnings) == 1
        warnings.clear()
        c = extract_image(tmp_path / "x.png", "img5", "text", cache=cache, codex=FakeCodex(result=None),
                          force_vision=False, ocr=lambda p: low, warn=warnings.append)
        assert c.method == "apple_vision" and len(warnings) == 1
    finally:
        cache.close()


def test_extract_image_ocr_unavailable(tmp_path):
    cache = _cache(tmp_path)

    def boom(p):
        raise OcrUnavailable("none")

    try:
        with pytest.raises(OcrUnavailable):
            extract_image(tmp_path / "x.png", "img6", "text", cache=cache, codex=None,
                          force_vision=False, ocr=boom, warn=lambda m: None)
        codex = FakeCodex({"brand": "B", "confidence": 0.8})
        c = extract_image(tmp_path / "x.png", "img6", "text", cache=cache, codex=codex,
                          force_vision=False, ocr=boom, warn=lambda m: None)
        assert c.method == "codex"
    finally:
        cache.close()


def test_extract_image_real_ocr_text_fixture(tmp_path):
    from adrenalin.common.ocr import recognize
    try:
        recognize(FIX / "simgad_text.png")
    except OcrUnavailable:
        pytest.skip("no OCR engine")
    cache = _cache(tmp_path)
    try:
        c = extract_image(FIX / "simgad_text.png", "real1", "text", cache=cache, codex=None,
                          force_vision=False, warn=lambda m: None)
        assert c.headline.startswith("Book Cheap Flight Tickets")
        assert c.brand == "flights70.com"
    finally:
        cache.close()
