"""Ad copy extraction: content.js scripts, OCR layout rules and Codex vision."""
from __future__ import annotations

import base64
import dataclasses
import gzip
import html
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from adrenalin.common.cache import ExtractionCache
from adrenalin.common.ocr import OcrLine, OcrResult, OcrUnavailable, recognize
from adrenalin.common.ocr.codex import CodexError, CodexRunner

CTA_WORDS = {"Install", "Download", "Get", "Open", "Learn more", "Visit site", "Shop now", "Sign up", "Book now"}
_CTA_LOWER = {w.lower() for w in CTA_WORDS}
OCR_METHODS = ("apple_vision", "tesseract")
_SKIP_STRINGS = {"Single Ad Rendering Service"}
_URL_RE = re.compile(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$")
_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")
_MARKUP_RE = re.compile(r">([^<>]+)<")
_JS_CODE_RE = re.compile(r"[{}\\]|=>|&&|\|\||\bfunction\b|\bvar\b|\.\w+\(|\w\(\w*\)|\w\[|\)[\w!;{)]|\)\s*[;{]|\(!")
_IMAGE_KEYS = ("squareImage", "landscapeImage", "portraitImage")


def log(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


@dataclass
class AdCopy:
    brand: str = ""
    display_url: str = ""
    website: str = ""
    landing_url: str = ""
    headline: str = ""
    description: str = ""
    cta: str = ""
    full_text: str = ""
    logo_url: str = ""
    video_id: str = ""
    method: str = ""
    model: str = ""
    sitelinks: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    confidence: float | None = None
    vision_pending: bool = False

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> AdCopy:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def website_from(display_url: str) -> str:
    s = display_url.strip()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s, flags=re.I)
    host = re.split(r"[/?#]", s, maxsplit=1)[0]
    if host.lower().startswith("www."):
        host = host[4:]
    return host


def _decode_escapes(js: str) -> str:
    s = _ESCAPE_RE.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), js)
    return s.replace("\u2066", "").replace("\u2069", "")


def _markup_strings(s: str) -> list[str]:
    out = []
    for m in _MARKUP_RE.finditer(s):
        t = html.unescape(m.group(1)).strip()
        if not re.search(r"[^\W\d_]", t) or _JS_CODE_RE.search(t) or t in _SKIP_STRINGS:
            continue
        out.append(t)
    return out


def _template_data(raw: str) -> dict[str, str]:
    """Key/value pairs of the first `'adData': [{...}]` block (parsed on the raw, still-escaped text)."""
    m = re.search(r"(?:'|\\x27)adData(?:'|\\x27)\s*:\s*\[\{", raw)
    if not m:
        return {}
    q = r"(?:'|\\x27)"
    pair = re.compile(
        q + r"(\w+)" + q + r"\s*:\s*" + q + r"(.*?)" + q + r"(?=\s*(?:,\s*" + q + r"\w+" + q + r"\s*:|\}))", re.S
    )
    block = raw[m.end():]
    end = re.search(r"\}\s*\]", block)
    block = block[: end.end()] if end else block
    return {k: _decode_escapes(v) for k, v in pair.findall("{" + block)}


def parse_content_js(js: str) -> AdCopy | None:
    c = AdCopy()
    found = False
    s = _decode_escapes(js)

    if m := re.search(r"ytimg\.com/vi/([\w-]+)/", s):
        c.video_id, found = m.group(1), True

    if m := re.search(r"adurl=(https?%3A[^&\"'\s\\<>]+)", s, re.I):
        c.landing_url, found = unquote(m.group(1)), True

    tpl = _template_data(js)
    if tpl:
        c.headline = tpl.get("headline", "")
        c.description = tpl.get("body", "")
        c.brand = tpl.get("advertiser", "")
        c.logo_url = tpl.get("logo", "")
        c.image_urls = [tpl[k] for k in _IMAGE_KEYS if tpl.get(k)]
        found = found or bool(c.headline or c.description or c.brand or c.logo_url or c.image_urls)

    strings = _markup_strings(s)
    if not c.video_id:
        if len(strings) >= 3 and not (c.headline or c.brand or c.description):
            c.brand = strings[0]
            rest = strings[1:]
            if _is_url(rest[0]):
                c.display_url = rest[0]
                rest = rest[1:]
            if rest:
                c.headline = rest[0]
                c.description = rest[1] if len(rest) > 1 else ""
                c.sitelinks = rest[2:]
            found = True
    elif strings:
        c.brand = c.brand or strings[0]
        c.description = c.description or max(strings, key=len)
        c.cta = next((t for t in strings if t.lower() in _CTA_LOWER), "")
        c.full_text = "\n".join(dict.fromkeys(strings))
        found = True

    if not found:
        return None
    c.method, c.confidence = "content_js", 1.0
    c.website = website_from(c.display_url) or (urlparse(c.landing_url).hostname or "")
    return c


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    n = shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, pos
        shift += 7
    raise ValueError("truncated varint")


def _overlay_field(data: bytes, name: str) -> str:
    marker = b"\n" + bytes([len(name)]) + name.encode() + b"\x12"
    i = data.find(marker)
    if i < 0:
        return ""
    try:
        pos = i + len(marker)
        _, pos = _read_varint(data, pos)  # outer length
        if data[pos : pos + 1] != b"\n":
            return ""
        n, pos = _read_varint(data, pos + 1)
        return data[pos : pos + n].decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def parse_overlay_param(content_js_url: str) -> AdCopy | None:
    vals = parse_qs(urlparse(content_js_url).query).get("overlay")
    if not vals:
        return None
    b64 = vals[0].lstrip("=")
    try:
        data = gzip.decompress(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
    except (ValueError, OSError, EOFError):
        return None
    headline = _overlay_field(data, "headline")
    description = _overlay_field(data, "description")
    display_url = _overlay_field(data, "visurl")
    if not (headline or description or display_url):
        return None
    return AdCopy(
        headline=headline, description=description, display_url=display_url,
        website=website_from(display_url), method="content_js", confidence=1.0,
    )


def _is_url(text: str) -> bool:
    return bool(_URL_RE.match(text.replace(" ", "")))


def parse_text_ad_ocr(r: OcrResult) -> AdCopy | None:
    lines = r.lines
    if len(lines) < 3 or not _is_url(lines[1].text):
        return None
    median = statistics.median(l.box[3] for l in lines)
    i = 2
    headline: list[str] = []
    while i < len(lines) and lines[i].box[3] >= 1.2 * median:
        headline.append(lines[i].text)
        i += 1
    if not headline:
        return None
    display_url = lines[1].text.replace(" ", "")
    return AdCopy(
        brand=lines[0].text, display_url=display_url, website=website_from(display_url),
        headline=" ".join(headline), description=" ".join(l.text for l in lines[i:]),
        full_text=r.text, method=r.method, confidence=r.confidence,
    )


def parse_image_ad_ocr(r: OcrResult) -> AdCopy:
    top = [l for l in r.lines if l.box[1] < 0.12]
    brand = top[0].text if top else ""
    display_url = next((l.text.replace(" ", "") for l in top if _is_url(l.text)), "")
    short = [l.text for l in r.lines if len(l.text.split()) <= 3 and re.search(r"[^\W\d_]", l.text)]
    cta = next((t for t in reversed(short) if t.lower() in _CTA_LOWER), short[-1] if short else "")
    return AdCopy(
        brand=brand, display_url=display_url, website=website_from(display_url), cta=cta,
        full_text=r.text, method=r.method, confidence=r.confidence if r.lines else None,
    )


def needs_vision(r: OcrResult, copy: AdCopy | None) -> bool:
    return not r.lines or r.confidence < 0.6 or copy is None


VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string"},
        "display_url": {"type": "string"},
        "headline": {"type": "string"},
        "description": {"type": "string"},
        "cta": {"type": "string"},
        "full_text": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["brand", "display_url", "headline", "description", "cta", "full_text", "confidence"],
    "additionalProperties": False,
}

VISION_PROMPT = (
    "This image is a Google ad. Read it and return JSON with: brand (advertiser name), display_url "
    "(the visible URL, empty if none), headline, description, cta (call-to-action button text, empty if none), "
    "full_text (all visible text, one line per text line) and confidence (0 to 1, how sure you are). "
    "Copy text exactly as shown. Use an empty string for anything not present."
)


def _ocr_to_dict(r: OcrResult) -> dict:
    return {
        "method": r.method,
        "lines": [{"text": l.text, "confidence": l.confidence, "box": list(l.box)} for l in r.lines],
    }


def _ocr_from_dict(d: dict) -> OcrResult:
    return OcrResult([OcrLine(l["text"], l["confidence"], tuple(l["box"])) for l in d["lines"]], d["method"])


def _vision_copy(d: dict, model: str) -> AdCopy:
    c = AdCopy.from_dict({k: v for k, v in d.items() if k not in ("method", "model", "website", "confidence")})
    c.website = website_from(c.display_url)
    c.method, c.model = "codex", model
    conf = d.get("confidence")
    c.confidence = float(conf) if isinstance(conf, (int, float)) else None
    return c


def extract_image(
    path: Path, image_id: str, fmt: str, *, cache: ExtractionCache, codex: CodexRunner | None,
    force_vision: bool, ocr=recognize, warn=log,
) -> AdCopy:
    is_text = str(fmt).lower() in ("text", "1")
    r: OcrResult | None = None
    ocr_failed = False
    pending = False

    def load_ocr() -> OcrResult:
        for method in OCR_METHODS:
            if (hit := cache.get(image_id, method)) is not None:
                return _ocr_from_dict(hit)
        res = ocr(Path(path))
        cache.put(image_id, res.method, "", _ocr_to_dict(res))
        return res

    def ocr_copy(res: OcrResult | None) -> AdCopy | None:
        if res is None:
            return None
        return parse_text_ad_ocr(res) if is_text else parse_image_ad_ocr(res)

    if not force_vision:
        try:
            r = load_ocr()
        except OcrUnavailable:
            if codex is None:
                raise
            ocr_failed = True
    copy = ocr_copy(r)

    if force_vision or ocr_failed or needs_vision(r, copy):
        if codex is not None:
            fields = cache.get(image_id, "codex", codex.model)
            if fields is None:
                try:
                    fields = codex.extract(Path(path), VISION_SCHEMA, VISION_PROMPT)
                    if fields is None:
                        pending = True
                        warn(f"vision limit reached; keeping OCR result for {image_id}")
                    elif not isinstance(fields, dict):
                        raise CodexError(f"unexpected codex output: {type(fields).__name__}")
                    else:
                        cache.put(image_id, "codex", codex.model, fields)
                except CodexError as e:
                    fields = None
                    warn(f"vision extraction failed for {image_id}: {e}; keeping OCR result")
            if fields is not None:
                return _vision_copy(fields, codex.model)
        if r is None and not ocr_failed:  # forced vision yielded nothing: fall back to OCR
            try:
                r = load_ocr()
            except OcrUnavailable:
                if codex is None:
                    raise
            copy = ocr_copy(r)

    if copy is None and r is not None and r.lines:
        copy = parse_image_ad_ocr(r)  # best effort when the text layout did not match
    if copy is None:
        copy = AdCopy()
    copy.vision_pending = pending
    return copy
