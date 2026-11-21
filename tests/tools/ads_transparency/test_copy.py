import json
from pathlib import Path

import pytest

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.ocr import OcrLine, OcrResult, OcrUnavailable
from adrenalin.common.ocr.codex import CodexError
from adrenalin.tools.ads_transparency.copy import (
    VISION_PROMPT,
    VISION_SCHEMA,
    VISION_VERSION,
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
_RECORDED = json.loads((FIX / "text_ads_ocr.json").read_text(encoding="utf-8"))


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
    """simgad_text.png as Apple Vision reads it, with the display URL read correctly."""
    d = _RECORDED["simgad_text.png"]
    lines = [OcrLine(l["text"], conf, tuple(l["box"])) for l in d["lines"]]
    lines[1].text = "www.flights70.com/"
    return OcrResult(lines, "apple_vision")


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
        cache.put("simgad:img1", "apple_vision", "", {"method": r.method, "lines": [
            {"text": l.text, "confidence": l.confidence, "box": list(l.box)} for l in r.lines]})
        c = extract_image(tmp_path / "x.png", "img1", "text", cache=cache, codex=None,
                          force_vision=False, ocr=_never, warn=lambda m: None)
        assert c.brand == "flights70.com" and c.method == "apple_vision"
    finally:
        cache.close()


def test_extract_image_cache_keys_are_namespaced(tmp_path):
    cache = _cache(tmp_path)
    try:
        extract_image(tmp_path / "x.png", "777", "text", cache=cache, codex=None, force_vision=False,
                      ocr=lambda p: _text_ad_result(), warn=lambda m: None)
        assert cache.get("simgad:777", "apple_vision") is not None and cache.get("777", "apple_vision") is None
        codex = FakeCodex({"brand": "B", "confidence": 0.9})
        extract_image(tmp_path / "x.png", "777", "text", cache=cache, codex=codex, force_vision=True,
                      ocr=_never, warn=lambda m: None)
        want = {"brand": "B", "confidence": 0.9}
        assert cache.get("simgad:777", "codex", f"gpt-5.6-luna#v{VISION_VERSION}") == want
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
        assert cache.get("simgad:img2", "codex", f"gpt-5.6-luna#v{VISION_VERSION}")["headline"] == "H"
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
        assert c.method == "apple_vision" and c.vision_pending and warnings == []  # the run summary counts it
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


def _page(*strings):
    return "".join(f"\\x3cdiv\\x3e{t}\\x3c/div\\x3e" for t in strings)


def test_content_js_text_ad_keeps_pipe_in_copy():
    c = parse_content_js(_page("Single Ad Rendering Service", "Acme", "www.acme.com/x", "Brand | Cheap Flights",
                               "Fly; now = save [today]", "Link A"))
    assert (c.brand, c.display_url, c.headline) == ("Acme", "www.acme.com/x", "Brand | Cheap Flights")
    assert c.description == "Fly; now = save [today]" and c.sitelinks == ["Link A"]


def test_content_js_text_ad_without_url_has_no_junk_display_url():
    c = parse_content_js(_page("Acme", "Great Headline", "A description here"))
    assert c.display_url == "" and c.brand == "Acme" and c.headline == "Great Headline"
    assert c.description == "A description here"


def test_content_js_code_noise_still_filtered():
    c = parse_content_js(_page("0&&m.h", "x.call(a)", "Acme", "www.acme.com", "Headline", "Desc"))
    assert c.brand == "Acme" and c.display_url == "www.acme.com"


def test_content_js_adData_not_overwritten_by_positional():
    raw = (FIX / "content_image.js").read_text(encoding="utf-8") + _page("Zed", "www.zed.com", "ZH", "ZD")
    c = parse_content_js(raw)
    assert c.brand == "Rent80" and c.display_url == "" and c.sitelinks == []


_CODEX_OK = {"brand": "B", "confidence": 0.9}


def test_forced_vision_codex_error_keeps_ocr(tmp_path):
    cache, warnings = _cache(tmp_path), []
    try:
        c = extract_image(tmp_path / "x.png", "f1", "text", cache=cache, codex=FakeCodex(exc=CodexError("x")),
                          force_vision=True, ocr=lambda p: _text_ad_result(), warn=warnings.append)
        assert c.method == "apple_vision" and c.brand == "flights70.com" and not c.vision_pending
        assert len(warnings) == 1
    finally:
        cache.close()


def test_forced_vision_limit_sets_pending(tmp_path):
    cache, warnings = _cache(tmp_path), []
    try:
        c = extract_image(tmp_path / "x.png", "f2", "text", cache=cache, codex=FakeCodex(result=None),
                          force_vision=True, ocr=lambda p: _text_ad_result(), warn=warnings.append)
        assert c.method == "apple_vision" and c.vision_pending is True and warnings == []
    finally:
        cache.close()


def test_needs_vision_limit_sets_pending(tmp_path):
    cache = _cache(tmp_path)
    try:
        c = extract_image(tmp_path / "x.png", "f3", "text", cache=cache, codex=FakeCodex(result=None),
                          force_vision=False, ocr=lambda p: _text_ad_result(0.4), warn=lambda m: None)
        assert c.vision_pending is True and c.method == "apple_vision"
        assert AdCopy.from_dict(c.to_dict()).vision_pending is True
    finally:
        cache.close()


def test_codex_non_dict_treated_as_error(tmp_path):
    cache, warnings = _cache(tmp_path), []
    try:
        c = extract_image(tmp_path / "x.png", "f4", "text", cache=cache, codex=FakeCodex(result=["bad"]),
                          force_vision=True, ocr=lambda p: _text_ad_result(), warn=warnings.append)
        assert c.method == "apple_vision" and len(warnings) == 1 and not c.vision_pending
    finally:
        cache.close()


# -- OCR layout of Text ads, on recorded Apple Vision output of live Lascade ads -----------------

def _recorded(name):
    d = _RECORDED[name]
    return OcrResult([OcrLine(l["text"], l["confidence"], tuple(l["box"])) for l in d["lines"]], d["method"])


_TEXT_ADS = {
    # wrapped headline "... Get closer" / "to Delhi" must not spill into the description
    "text_ad_5464025061462758635.png": dict(
        brand="flights70.com", display_url="www.flights70.com/", website="flights70.com",
        headline="Book flights from Delhi - Get closer to Delhi",
        description="With our flight comparison tool, you get the best deals from 100+ travel websites",
        sitelinks=[]),
    # logo glyphs "H" / "Huskiro" between brand and URL, URL read at confidence 0.3, pills and a callout
    "text_ad_11321256415929196548.png": dict(
        brand="Hotels70", display_url="www.hotels70.com/", website="hotels70.com",
        headline="Hotels in Bergen op Zoom from €29 - Hotels from €29",
        description="Book last-minute hotels from €29, with flexible cancellation options. Discover hotels from "
                    "€29/night with no hidden fees. Quick and secure booking. Book Now. Great Offers. ...",
        sitelinks=["Hotels in Bergen op Zoom", "Book Cheap Hotels fror", "Up to 70% off on all Hotel Booking"]),
    # no brand line above the URL
    "text_ad_5232610464116262506.png": dict(
        brand="", display_url="www.marineradar.com/", website="marineradar.com",
        headline="Track This Ship Live",
        description="See where this ship is now: live position, speed, course and destination.",
        sitelinks=[]),
    # headline's second line only 1.17x the median line height; list sitelinks with ">" chevrons
    "text_ad_7828145789531681851.png": dict(
        brand="marineradar.com", display_url="www.marineradar.com/", website="marineradar.com",
        headline="Live Ship Map - Track Any Ship by Name",
        description="Out of coastal range? 1 satellite credit fetches its latest mid-ocean position.",
        sitelinks=["Live Ship Map", "Live Vessel Position", "Container Tracking", "Ports Directory", "Get the App"]),
    # Rio, 348 px render: "Sponsored" label above, logo text "70" ending right of x = 0.09, below the brand line
    "text_ad_9688646368325413885.png": dict(
        brand="Save 70", display_url="www.save70.com/", website="save70.com", headline="$49 Flights",
        description="Cheapest Flight Deals? Last Minute Price Drops on Intl. & Domestic Airfare. Compare Today!",
        sitelinks=[]),
    # logo text "TRAVEL" level with the URL line
    "text_ad_6017471862956656205.png": dict(
        brand="Save70", display_url="www.save70.com/", website="save70.com", headline="70% Off - Cheap Car Rental",
        description="2025 Car Rental Deals in <dynamically generated location> from $7.95/Day. "
                    "Book Easily & Quickly Online!",
        sitelinks=[]),
    # German "Gesponsert" label and no brand line: the label is not the brand
    "text_ad_6440410688637658213.png": dict(
        brand="", display_url="www.cars-rentals.com/", website="cars-rentals.com", headline="70 Rabatt Mietwagen",
        description="Vergleiche Mietwagen ab €7,95/Tag. Erspare bis 70% noch Heute. Schnelle Online-Buchung",
        sitelinks=[]),
    # logo and brand read as one garbled line ("→ neares com"): left blank rather than wrong
    "text_ad_10954299470998847669.png": dict(
        brand="", display_url="www.cheapfares.com/", website="cheapfares.com", headline="70% Off Flights From",
        description="Cheapest Flight Deals? Last Minute Price Drops on Intl. & Domestic Airfare. Compare Today!",
        sitelinks=[]),
}


def _fields(c):
    return {k: getattr(c, k) for k in ("brand", "display_url", "website", "headline", "description", "sitelinks")}


@pytest.mark.parametrize("name", list(_TEXT_ADS))
def test_parse_text_ad_ocr_live_layouts(name):
    r = _recorded(name)
    c = parse_text_ad_ocr(r)
    assert _fields(c) == _TEXT_ADS[name]
    assert c.method == "apple_vision" and c.confidence == r.confidence and not needs_vision(r, c)


@pytest.mark.parametrize("name", list(_TEXT_ADS))
def test_parse_text_ad_real_ocr_matches_recording(name):
    from adrenalin.common.ocr import recognize
    try:
        r = recognize(FIX / name)
    except OcrUnavailable:
        pytest.skip("no OCR engine")
    if r.method != "apple_vision":
        pytest.skip("expected values are for Apple Vision")
    want = _TEXT_ADS[name]
    c = parse_text_ad_ocr(r)
    assert (c.brand, c.website, c.headline) == (want["brand"], want["website"], want["headline"])


def test_misread_url_host_without_dot_goes_to_codex_and_is_never_emitted():
    # simgad_text.png: Apple Vision reads www.flights70.com/ as "www.tlights/0.com/" at confidence 1.0
    r = _recorded("simgad_text.png")
    assert r.lines[1].text == "www.tlights/0.com/" and r.confidence == 1.0
    c = parse_text_ad_ocr(r)
    assert (c.brand, c.display_url, c.website) == ("flights70.com", "", "flights70.com")  # brand domain wins
    assert c.headline == "Book Cheap Flight Tickets - Find Cheapest Flight Tickets"
    assert c.description.endswith("Search Flights. Affordable Rates.")
    assert c.confidence <= 0.5 and needs_vision(r, c)


def _ad(brand, url):
    return OcrResult([
        OcrLine(brand, 1.0, (0.11, 0.0, 0.2, 0.08)),
        OcrLine(url, 1.0, (0.11, 0.1, 0.3, 0.06)),
        OcrLine("Cheap Flights on Flights70 - Save", 1.0, (0.0, 0.22, 0.95, 0.09)),
        OcrLine("Upto 70% on flight ticket", 1.0, (0.0, 0.34, 0.7, 0.1)),
        OcrLine("Flights70 helps travellers make", 1.0, (0.0, 0.5, 0.55, 0.06)),
        OcrLine("smarter flight booking decisions.", 1.0, (0.0, 0.59, 0.58, 0.06)),
    ], "apple_vision")


def test_misread_url_without_brand_domain_blanks_website():
    r = _ad("Flights70", "www.flights/0.com/")
    c = parse_text_ad_ocr(r)
    assert (c.brand, c.display_url, c.website) == ("Flights70", "", "")
    assert c.headline == "Cheap Flights on Flights70 - Save Upto 70% on flight ticket"
    assert c.description == "Flights70 helps travellers make smarter flight booking decisions."
    assert needs_vision(r, c)


def test_brand_domain_disagreeing_with_url_prefers_brand_and_asks_codex():
    r = _ad("flights70.com", "www.fiights70.com/")
    c = parse_text_ad_ocr(r)
    assert (c.display_url, c.website) == ("www.fiights70.com/", "flights70.com")
    assert needs_vision(r, c)
    sub = parse_text_ad_ocr(_ad("flights70.com", "app.flights70.com/"))
    assert (sub.website, sub.confidence) == ("app.flights70.com", 1.0)


def test_text_ad_wrapped_hyphenated_word_is_rejoined():
    r = _ad("Rent80", "app.rent80.com/")
    r.lines[4] = OcrLine("Rent a car stress-", 1.0, (0.0, 0.5, 0.3, 0.06))
    r.lines[5] = OcrLine("free with Rent80.", 1.0, (0.0, 0.59, 0.28, 0.06))
    assert parse_text_ad_ocr(r).description == "Rent a car stress-free with Rent80."


def test_text_ad_layout_mismatch_falls_back_without_inventing_cta(tmp_path):
    r = OcrResult([OcrLine("flights70.com", 1.0, (0.11, 0.0, 0.2, 0.08)),
                   OcrLine("www.flights70.com/", 1.0, (0.11, 0.1, 0.3, 0.06)),
                   OcrLine("Book now", 1.0, (0.0, 0.3, 0.1, 0.05))], "apple_vision")
    assert parse_text_ad_ocr(r) is None
    cache = _cache(tmp_path)
    try:
        c = extract_image(tmp_path / "x.png", "m1", "text", cache=cache, codex=None, force_vision=False,
                          ocr=lambda p: r, warn=lambda m: None)
        assert (c.brand, c.display_url, c.cta) == ("flights70.com", "www.flights70.com/", "")
    finally:
        cache.close()


class FakeHosts:
    def __init__(self, missing=()):
        self.missing, self.calls = set(missing), []

    def __call__(self, host):
        self.calls.append(host)
        return host.removeprefix("www.") not in self.missing


def test_unresolvable_ocr_host_falls_back_to_brand_domain_and_codex(tmp_path):
    cache, hosts = _cache(tmp_path), FakeHosts({"flights7o.com"})
    try:
        r = _ad("flights70.com", "www.flights7O.com/")
        c = extract_image(tmp_path / "x.png", "h1", "text", cache=cache, codex=None, force_vision=False,
                          ocr=lambda p: r, warn=lambda m: None, host_exists=hosts)
        assert (c.display_url, c.website, c.method) == ("", "flights70.com", "apple_vision")
        assert c.confidence <= 0.5 and "www.flights7o.com" in hosts.calls
        codex = FakeCodex({"brand": "Flights70", "display_url": "www.flights70.com/", "confidence": 0.9})
        c = extract_image(tmp_path / "x.png", "h2", "text", cache=cache, codex=codex, force_vision=False,
                          ocr=lambda p: r, warn=lambda m: None, host_exists=hosts)
        assert (c.method, c.website) == ("codex", "flights70.com")
    finally:
        cache.close()


def test_unresolvable_host_without_brand_domain_blanks_website(tmp_path):
    cache, hosts = _cache(tmp_path), FakeHosts({"flights7o.com"})
    try:
        c = extract_image(tmp_path / "x.png", "h3", "text", cache=cache, codex=None, force_vision=False,
                          ocr=lambda p: _ad("Flights70", "www.flights7O.com/"), warn=lambda m: None,
                          host_exists=hosts)
        assert (c.display_url, c.website) == ("", "") and c.confidence <= 0.5
    finally:
        cache.close()


def test_resolvable_host_keeps_ocr_copy(tmp_path):
    cache, hosts = _cache(tmp_path), FakeHosts()
    try:
        c = extract_image(tmp_path / "x.png", "h4", "text", cache=cache, codex=FakeCodex(exc=AssertionError()),
                          force_vision=False, ocr=lambda p: _ad("Flights70", "www.flights70.com/"),
                          warn=lambda m: None, host_exists=hosts)
        assert (c.display_url, c.website, c.confidence) == ("www.flights70.com/", "flights70.com", 1.0)
        assert hosts.calls == ["www.flights70.com"]  # the host as shown, with its www.
    finally:
        cache.close()


# -- no OCR engine: an image nobody could read fails (retried next run) instead of a blank row ----

def _no_ocr(p):
    raise OcrUnavailable("No OCR engine")


@pytest.mark.parametrize("codex", [FakeCodex(exc=CodexError("codex exited 1")), FakeCodex(result=None)],
                         ids=["codex-error", "codex-limit"])
@pytest.mark.parametrize("force", [False, True], ids=["ocr-first", "forced-vision"])
def test_no_ocr_and_no_codex_result_raises(tmp_path, codex, force):
    cache = _cache(tmp_path)
    try:
        with pytest.raises(OcrUnavailable):
            extract_image(tmp_path / "x.png", "n1", "text", cache=cache, codex=codex, force_vision=force,
                          ocr=_no_ocr, warn=lambda m: None)
    finally:
        cache.close()


def test_missing_codex_warns_once(tmp_path):
    from adrenalin.common.ocr.codex import CodexMissing

    class Missing(FakeCodex):
        missing = False

        def extract(self, image, schema, prompt):
            self.calls += 1
            self.missing = True
            raise CodexMissing("codex binary not found")

    cache, warnings, codex = _cache(tmp_path), [], Missing()
    try:
        for i in range(3):
            c = extract_image(tmp_path / "x.png", f"w{i}", "text", cache=cache, codex=codex, force_vision=False,
                              ocr=lambda p: _text_ad_result(0.4), warn=warnings.append)
            assert c.method == "apple_vision"
        assert codex.calls == 1 and len(warnings) == 1
    finally:
        cache.close()


# -- Codex output ---------------------------------------------------------------------------------

def test_vision_schema_asks_for_sitelinks_and_button_only_cta():
    assert VISION_SCHEMA["properties"]["sitelinks"] == {"type": "array", "items": {"type": "string"}}
    assert set(VISION_SCHEMA["required"]) == set(VISION_SCHEMA["properties"])
    assert "sitelinks" in VISION_PROMPT and "button" in VISION_PROMPT


def test_vision_copy_collapses_whitespace_except_full_text(tmp_path):
    cache = _cache(tmp_path)
    fields = {"brand": "Hotels70", "display_url": "www.hotels70.com/", "headline": "Stop Overpaying - Price\nDrop",
              "description": "Don't just book,\n discover!  Hotels70", "cta": "",
              "sitelinks": ["Last-Minute\nDeals", " ", "Book Hotels70"], "full_text": "Hotels70\nStop", "confidence": 1}
    try:
        c = extract_image(tmp_path / "x.png", "v1", "text", cache=cache, codex=FakeCodex(fields),
                          force_vision=True, ocr=_never, warn=lambda m: None)
        assert (c.headline, c.description) == ("Stop Overpaying - Price Drop", "Don't just book, discover! Hotels70")
        assert c.sitelinks == ["Last-Minute Deals", "Book Hotels70"] and c.cta == ""
        assert c.full_text == "Hotels70\nStop" and c.confidence == 1.0 and c.model == "gpt-5.6-luna"
    finally:
        cache.close()


def test_codex_cache_is_versioned(tmp_path):
    cache = _cache(tmp_path)
    try:
        # a result cached before sitelinks were asked for is not reused
        cache.put("simgad:v2", "codex", "gpt-5.6-luna", {"brand": "Old", "cta": "Last-Minute Deals"})
        cache.put("v2", "codex", "gpt-5.6-luna", {"brand": "Old", "cta": "Last-Minute Deals"})
        codex = FakeCodex({"brand": "New", "sitelinks": ["A link"], "confidence": 0.9})
        c = extract_image(tmp_path / "x.png", "v2", "text", cache=cache, codex=codex, force_vision=True,
                          ocr=_never, warn=lambda m: None)
        assert (c.brand, c.sitelinks, codex.calls, c.model) == ("New", ["A link"], 1, "gpt-5.6-luna")
    finally:
        cache.close()


# -- content.js video / app-install copy ----------------------------------------------------------

def _video(*strings):
    return "x ytimg.com/vi/VID123/hq.jpg " + _page(*strings)


_STORE_INFO = ("DOWNLOADS", "Downloads", "CATEGORY", "Category", "DEVELOPER", "Lascade")


def test_video_description_joins_wrapped_fragments_and_headline_follows_brand():
    # strings of live row ...571713
    c = parse_content_js(_video("MarineRadar", "Simplify Marine Work", *_STORE_INFO,
                                "Access ship details fast. Enhance your", "maritime operations with ease!",
                                "Install", "MarineRadar:", "Vessel Tracker"))
    assert (c.brand, c.headline, c.cta) == ("MarineRadar", "Simplify Marine Work", "Install")
    assert c.description == "Access ship details fast. Enhance your maritime operations with ease!"
    # live row ...780481: the wrapped part starts with "|"
    c = parse_content_js(_video("MarineRadar", "Vessel Finder Online", *_STORE_INFO,
                                "Real-time ship tracking online | Vessel Finder", "| Instant Marine Traffic",
                                "Install", "MarineRadar:", "Vessel Tracker"))
    assert c.headline == "Vessel Finder Online"
    assert c.description == "Real-time ship tracking online | Vessel Finder | Instant Marine Traffic"


def test_video_without_headline_or_description():
    # live row ...420609: fragments of the description are not a headline
    c = parse_content_js(_video("Cheap Hotels・Hotels70", "Save up to 70% on Hotel Booking", "Online!",
                                "Save up to 70% on Hotel Booking Online!", "Install", "Cheap Hotels・", "Hotels70"))
    assert (c.headline, c.description) == ("", "Save up to 70% on Hotel Booking Online!")
    # live rows ...358529: only the app title after the button, which is not a description
    c = parse_content_js(_video("eSIM70", "Install", "eSIM70: eSIM", "For Travel Data"))
    assert (c.brand, c.headline, c.description, c.cta) == ("eSIM70", "", "", "Install")
    assert c.full_text == "eSIM70\nInstall\neSIM70: eSIM\nFor Travel Data"


def test_video_fixture_has_no_headline():
    c = parse_content_js((FIX / "content_video.js").read_text(encoding="utf-8"))
    assert c.headline == ""


_IMAGE_ONLY = "var a = {'adData': [{'squareImage': 'https://tpc.googlesyndication.com/simgad/12147114744176439645'}]};"


def test_content_js_with_images_but_no_copy_is_not_confident():
    from adrenalin.tools.ads_transparency.copy import parse_content_preview

    c = parse_content_preview(_IMAGE_ONLY, "https://x.example/content.js?assets=abc")
    assert c.image_urls == ["https://tpc.googlesyndication.com/simgad/12147114744176439645"]
    assert not c.has_copy and c.confidence is None and c.method == "content_js"
    # the overlay= parameter supplies the copy when the script has none
    c = parse_content_preview(_IMAGE_ONLY, OVERLAY_URL)
    assert c.has_copy and c.headline.startswith("eSIM for 190+ Countries") and c.confidence == 1.0
    assert c.image_urls == ["https://tpc.googlesyndication.com/simgad/12147114744176439645"]
    assert parse_content_preview("var x = 1;", "https://x.example/content.js") is None


def test_vision_copy_tolerates_non_string_fields(tmp_path):
    cache = _cache(tmp_path)
    try:
        c = extract_image(tmp_path / "x.png", "v3", "text", cache=cache, force_vision=True, ocr=_never,
                          codex=FakeCodex({"brand": None, "cta": 3, "sitelinks": [None, "A"], "confidence": "x"}),
                          warn=lambda m: None)
        assert (c.brand, c.cta, c.sitelinks, c.confidence) == ("", "", ["A"], None)
    finally:
        cache.close()


def test_video_all_caps_copy_is_not_a_store_label():
    c = parse_content_js(_video("Brand", "BIG SALE", "Up to 70% off hotels today.", "Install", "App"))
    assert (c.headline, c.description, c.confidence) == ("BIG SALE", "Up to 70% off hotels today.", 1.0)
    c = parse_content_js(_video("Brand", "Hotels from 29 EUR", "SAVE 70% ON HOTELS",
                                "Book now and save big on stays.", "Install", "App"))
    assert (c.headline, c.description) == ("SAVE 70% ON HOTELS", "Book now and save big on stays.")


# -- Broken Preview -------------------------------------------------------------------------------

BROKEN_TEXT = ["500.", "That’s an error.", "There was an error. Please try again later."]


def _broken_ocr(path):
    return OcrResult([OcrLine(t, 0.95, (0.1, 0.1 + i * 0.1, 0.5, 0.05)) for i, t in enumerate(BROKEN_TEXT)],
                     "apple_vision")


def test_is_broken_preview():
    from adrenalin.tools.ads_transparency.copy import is_broken_preview

    assert is_broken_preview("500. That’s an error. | There was an error. Please try again later.")
    assert is_broken_preview("500. That's an error. | There was an error. Please try again later.")
    assert not is_broken_preview("$49 Flights")
    assert not is_broken_preview("That's an error")
    assert not is_broken_preview("")


def test_broken_ocr_skips_codex(tmp_path):
    cache, codex = _cache(tmp_path), FakeCodex({"brand": "B", "confidence": 0.9})
    try:
        c = extract_image(tmp_path / "x.png", "br1", "text", cache=cache, codex=codex, force_vision=False,
                          ocr=_broken_ocr, warn=lambda m: None)
    finally:
        cache.close()
    assert c.preview_broken and codex.calls == 0
    assert not (c.brand or c.headline or c.description or c.display_url or c.full_text)


def test_broken_codex_result_is_a_broken_preview(tmp_path):
    cache = _cache(tmp_path)
    codex = FakeCodex({"headline": "500. That’s an error.", "full_text": "Please try again later.",
                       "confidence": 0.9})
    try:
        c = extract_image(tmp_path / "x.png", "br2", "text", cache=cache, codex=codex, force_vision=True,
                          ocr=_never, warn=lambda m: None)
    finally:
        cache.close()
    assert c.preview_broken and codex.calls == 1 and not c.headline and not c.full_text


@pytest.mark.parametrize("store", ["Google Play", "App Store"])
def test_store_badge_is_not_a_headline(store):
    from adrenalin.tools.ads_transparency.copy import _video_headline_description

    strings = ["Hotels70", store, "Cheap hotel deals tonight", "Install"]
    headline, description = _video_headline_description(strings, "Hotels70")
    assert headline != store
    assert store not in description
    assert description == "Cheap hotel deals tonight"


def test_video_renderer2_parsed():
    c = parse_content_js((FIX / "content_video_renderer2.js").read_text(encoding="utf-8"))
    assert c is not None
    assert c.video_id == "fvu_CcMOAc4"
    assert c.brand == "Hotels70"
    assert c.headline == "Cheap Hotels・Hotels70"
    assert c.description.startswith("Cheap hotels anytime!")
    assert c.method == "content_js"


def test_forced_vision_uses_cached_broken_ocr_before_codex(tmp_path):
    cache, codex = _cache(tmp_path), FakeCodex({"brand": "B", "confidence": 0.9})
    try:
        extract_image(tmp_path / "x.png", "br3", "text", cache=cache, codex=codex, force_vision=False,
                      ocr=_broken_ocr, warn=lambda m: None)  # the OCR read is cached
        c = extract_image(tmp_path / "x.png", "br3", "text", cache=cache, codex=codex, force_vision=True,
                          ocr=_never, warn=lambda m: None)
    finally:
        cache.close()
    assert c.preview_broken and codex.calls == 0


@pytest.mark.parametrize("field", ["brand", "description"])
def test_broken_codex_result_checked_in_brand_and_description(tmp_path, field):
    cache = _cache(tmp_path)
    codex = FakeCodex({field: "500. That's an error. Please try again later.", "confidence": 0.9})
    try:
        c = extract_image(tmp_path / "x.png", "br4", "text", cache=cache, codex=codex, force_vision=True,
                          ocr=_never, warn=lambda m: None)
    finally:
        cache.close()
    assert c.preview_broken and not getattr(c, field)
