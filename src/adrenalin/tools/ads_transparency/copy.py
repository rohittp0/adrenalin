"""Ad copy extraction: content.js scripts, OCR layout rules and Codex vision."""
from __future__ import annotations

import base64
import dataclasses
import gzip
import html
import re
import sys
from collections.abc import Callable
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

    @property
    def has_copy(self) -> bool:
        return bool(self.brand or self.headline or self.description or self.display_url)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> AdCopy:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def _host(url: str) -> str:
    """The host of a URL as shown, `www.` included, lowercased."""
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip(), flags=re.I)
    return re.split(r"[/?#]", s, maxsplit=1)[0].lower()


def website_from(display_url: str) -> str:
    host = _host(display_url)
    return host[4:] if host.startswith("www.") else host


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
            if _url_like(rest[0]):
                c.display_url = rest[0]
                rest = rest[1:]
            if rest:
                c.headline = rest[0]
                c.description = rest[1] if len(rest) > 1 else ""
                c.sitelinks = rest[2:]
            found = True
    elif strings:
        c.brand = c.brand or strings[0]
        headline, description = _video_headline_description(strings, c.brand)
        c.headline, c.description = c.headline or headline, c.description or description
        c.cta = next((t for t in strings if t.lower() in _CTA_LOWER), "")
        c.full_text = "\n".join(dict.fromkeys(strings))
        found = True

    if not found:
        return None
    c.method, c.confidence = "content_js", 1.0 if c.has_copy else None
    c.website = website_from(c.display_url) or (urlparse(c.landing_url).hostname or "")
    return c


# Play Store info labels in app-install Previews; each is followed by its value. Matched exactly: other
# all-caps strings ("BIG SALE") are ad copy.
_STORE_LABELS = {"DOWNLOADS", "CATEGORY", "DEVELOPER", "RATING", "RATINGS", "REVIEWS", "INSTALLS", "SIZE", "PRICE"}


def _video_headline_description(strings: list[str], brand: str) -> tuple[str, str]:
    """Headline and description of a video / app-install Preview's markup strings. The layout is brand,
    [headline], [store info: DOWNLOADS/CATEGORY/DEVELOPER label + value], description (wrapped over several
    strings, often repeated whole), the CTA button, then the app title, which is neither."""
    cta = next((i for i, t in enumerate(strings) if t.lower() in _CTA_LOWER), len(strings))
    body: list[str] = []
    label = False
    for t in strings[:cta]:
        if label:
            label = False  # the value after a store-info label
        elif t == brand:
            continue
        elif t in _STORE_LABELS:
            label = True
        elif body and not re.search(r"[.!?…:]$", body[-1]) and re.match(r"[a-z|&,–—-]", t):
            body[-1] = f"{body[-1]} {t}"  # a wrapped sentence: "... Enhance your" + "maritime operations ..."
        else:
            body.append(t)
    if not body:
        return "", ""
    description = max(body, key=len)
    before = body[:body.index(description)]
    headline = next((t for t in reversed(before) if t not in description), "")
    return headline, description


def parse_content_preview(js: str, url: str) -> AdCopy | None:
    """Copy of a `content.js` Preview: the script, plus the URL's `overlay=` text when the script has no copy."""
    c = parse_content_js(js)
    if c is not None and c.has_copy:
        return c
    overlay = parse_overlay_param(url)
    if overlay is None or c is None:
        return overlay or c
    c.headline, c.description, c.display_url = overlay.headline, overlay.description, overlay.display_url
    c.website = overlay.website or c.website
    c.confidence = overlay.confidence
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


# Text-ad layout (Google's mobile search-ad render, about 700 px wide). Measured on live Apple Vision output:
# headline text is 0.025-0.043 image widths per character, description and sitelinks 0.014-0.020 (callouts and
# list sitelinks up to 0.025, but they follow the description). Gaps inside a block are at most 0.73 line
# heights, gaps before sitelinks at least 1.5. Logo glyphs ("H", "RENT", "80") end left of x = 0.07, while
# brand and URL lines start at x >= 0.09 and body lines at x = 0.
_HEADLINE_CHAR_WIDTH = 0.022
_BLOCK_GAP = 1.1
_LOGO_EDGE = 0.09
MIN_CONFIDENCE = 0.6
_DOUBTFUL = 0.5  # confidence given to OCR copy whose domain looks misread, so it goes to Codex


def _url_like(text: str) -> bool:
    """Shaped like a URL, possibly misread: `www.tlights/0.com/` still marks the URL line."""
    return bool(_URL_RE.match(text.replace(" ", "")))


def _domain(text: str) -> str:
    """The host of a well-formed URL or domain (its host has a dot once `www.` is removed), else ""."""
    host = website_from(text.replace(" ", "")) if _url_like(text) else ""
    return host if "." in host else ""


def _same_site(a: str, b: str) -> bool:
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _doubt(c: AdCopy) -> None:
    c.confidence = _DOUBTFUL if c.confidence is None else min(c.confidence, _DOUBTFUL)


def _rows(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """Group lines into visual rows (side-by-side sitelink pills, a sitelink and its ">"), each sorted by x."""
    rows: list[list[OcrLine]] = []
    for l in lines:
        mid = l.box[1] + l.box[3] / 2
        first = rows[-1][0] if rows else None
        if first is not None and first.box[1] <= mid <= first.box[1] + first.box[3]:
            rows[-1].append(l)
        else:
            rows.append([l])
    return [sorted(r, key=lambda l: l.box[0]) for r in rows]


def _gap(a: list[OcrLine], b: list[OcrLine]) -> float:
    """Vertical gap between two rows, in line heights of the smaller one."""
    bottom = max(l.box[1] + l.box[3] for l in a)
    height = min(min(l.box[3] for l in a), min(l.box[3] for l in b))
    return (min(l.box[1] for l in b) - bottom) / height if height > 0 else 0.0


def _headline_font(row: list[OcrLine]) -> bool:
    text = row[0].text.strip()
    return len(row) == 1 and bool(text) and row[0].box[2] / len(text) >= _HEADLINE_CHAR_WIDTH


def _block(rows: list[list[OcrLine]], start: int, more) -> int:
    """End index of the block of single-line rows from `start` whose gaps stay small and `more` holds."""
    i = start
    while (i < len(rows) and len(rows[i]) == 1 and more(rows[i])
           and (i == start or _gap(rows[i - 1], rows[i]) <= _BLOCK_GAP)):
        i += 1
    return i


def _join(rows: list[list[OcrLine]]) -> str:
    out = ""
    for row in rows:
        t = row[0].text.strip()
        if re.search(r"[^\W\d_]-$", out) and t[:1].islower():
            out += t  # "stress-" + "free"
        else:
            out = f"{out} {t}" if out else t
    return out


def _sitelink(text: str) -> str:
    t = text.strip().lstrip("•·").strip()
    # ">" chevrons and slivers of pills cut off at the image edge ("Ca") are not sitelinks
    return t if sum(ch.isalpha() for ch in t) >= 3 else ""


def parse_text_ad_ocr(r: OcrResult) -> AdCopy | None:
    """Brand, display URL, headline, description and sitelinks of a Text-format image, or None when the
    lines don't follow that layout. A display URL that looks misread is never emitted and lowers the confidence."""
    lines = [l for l in r.lines if l.text.strip()]
    n = len(lines)
    # The URL is the last URL-like line of the header: the brand line above it may be a domain too.
    u = next((i for i in range(min(n, 5))
              if _url_like(lines[i].text) and not (i + 1 < n and _url_like(lines[i + 1].text))), None)
    if u is None:
        return None
    url = lines[u]
    header_bottom = url.box[1] + url.box[3]

    def logo(l: OcrLine) -> bool:
        return l.box[0] + l.box[2] < _LOGO_EDGE and l.box[1] < header_bottom

    above = [l for l in lines[:u] if not logo(l)]
    brand = above[-1].text.strip() if above else ""
    rows = _rows([l for l in lines[u + 1:] if not logo(l)])
    h = _block(rows, 0, _headline_font)
    d = _block(rows, h, lambda row: True)
    if h == 0 or d == h:
        return None

    shown = url.text.replace(" ", "")
    host, brand_host = website_from(shown), _domain(brand)
    if "." not in host:  # e.g. "www.tlights/0.com/"
        display_url, website = "", brand_host
    elif brand_host and not _same_site(host, brand_host):
        display_url, website = shown, brand_host
    else:
        display_url, website = shown, host
    c = AdCopy(
        brand=brand, display_url=display_url, website=website, headline=_join(rows[:h]),
        description=_join(rows[h:d]), full_text=r.text, method=r.method, confidence=r.confidence,
        sitelinks=[t for row in rows[d:] for l in row if (t := _sitelink(l.text))],
    )
    if display_url != shown or website != host:
        _doubt(c)
    return c


def parse_image_ad_ocr(r: OcrResult) -> AdCopy:
    top = [l for l in r.lines if l.box[1] < 0.12]
    brand = top[0].text if top else ""
    urls = [l.text.replace(" ", "") for l in top if _domain(l.text)]
    display_url = urls[-1] if urls else ""  # the brand line above the URL can be a domain too
    short = [l.text for l in r.lines if len(l.text.split()) <= 3 and re.search(r"[^\W\d_]", l.text)]
    cta = next((t for t in reversed(short) if t.lower() in _CTA_LOWER), short[-1] if short else "")
    return AdCopy(
        brand=brand, display_url=display_url, website=website_from(display_url), cta=cta,
        full_text=r.text, method=r.method, confidence=r.confidence if r.lines else None,
    )


def verify_hosts(c: AdCopy, host_exists) -> None:
    """Check OCR-read domains on the network: one that doesn't resolve was misread. It is dropped (the
    website falls back to the brand line's domain when that one resolves) and the copy goes to Codex."""
    bad: set[str] = set()
    url_host = website_from(c.display_url)
    # the host as shown, with its www. (the checker also tries the other form)
    if url_host and not host_exists(_host(c.display_url)):
        bad.add(url_host)
        c.display_url = ""
    if c.website and (c.website in bad or (c.website != url_host and not host_exists(c.website))):
        bad.add(c.website)
        brand_host = _domain(c.brand)
        c.website = brand_host if brand_host and brand_host not in bad and host_exists(brand_host) else ""
    if bad:
        _doubt(c)


def needs_vision(r: OcrResult, copy: AdCopy | None) -> bool:
    return (not r.lines or r.confidence < MIN_CONFIDENCE or copy is None
            or (copy.confidence is not None and copy.confidence < MIN_CONFIDENCE))


VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string"},
        "display_url": {"type": "string"},
        "headline": {"type": "string"},
        "description": {"type": "string"},
        "cta": {"type": "string"},
        "sitelinks": {"type": "array", "items": {"type": "string"}},
        "full_text": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["brand", "display_url", "headline", "description", "cta", "sitelinks", "full_text", "confidence"],
    "additionalProperties": False,
}
# Bump when the schema or prompt changes: cached Codex results of older versions are then not reused.
VISION_VERSION = 2

VISION_PROMPT = (
    "This image is a Google ad. Read it and return JSON with: brand (advertiser name), display_url "
    "(the visible URL, empty if none), headline, description, cta (the text of a call-to-action button such as "
    "Install or Shop now; empty if there is no button, as in search/text ads), sitelinks (the extra links and "
    "callouts shown under a search ad, such as link pills, a list of links or a tagged line, one string each, "
    "in reading order; empty list if none), full_text (all visible text, one line per text line) and "
    "confidence (0 to 1, how sure you are). Copy text exactly as shown. Use an empty string for anything not present."
)


def _ocr_to_dict(r: OcrResult) -> dict:
    return {
        "method": r.method,
        "lines": [{"text": l.text, "confidence": l.confidence, "box": list(l.box)} for l in r.lines],
    }


def _ocr_from_dict(d: dict) -> OcrResult:
    return OcrResult([OcrLine(l["text"], l["confidence"], tuple(l["box"])) for l in d["lines"]], d["method"])


def _squash(text) -> str:
    return " ".join(text.split()) if isinstance(text, str) else ""


def _vision_copy(d: dict, model: str) -> AdCopy:
    c = AdCopy.from_dict({k: v for k, v in d.items()
                          if k not in ("method", "model", "website", "confidence", "sitelinks")})
    for f in ("brand", "display_url", "headline", "description", "cta"):
        setattr(c, f, _squash(getattr(c, f)))
    links = d.get("sitelinks")
    c.sitelinks = [t for s in links if (t := _squash(s))] if isinstance(links, list) else []
    c.website = website_from(c.display_url)
    c.method, c.model = "codex", model
    conf = d.get("confidence")
    c.confidence = float(conf) if isinstance(conf, (int, float)) else None
    return c


def _vision(path: Path, key: str, cache: ExtractionCache, codex: CodexRunner | None, warn) -> tuple[dict | None, bool]:
    """Codex fields for an image (cached per model and VISION_VERSION), and whether the call cap deferred it."""
    if codex is None:
        return None, False
    version = f"{codex.model}#v{VISION_VERSION}"
    if (hit := cache.get(key, "codex", version)) is not None:
        return hit, False
    if getattr(codex, "missing", False):
        return None, False  # codex is not installed: already reported once
    try:
        fields = codex.extract(Path(path), VISION_SCHEMA, VISION_PROMPT)
    except CodexError as e:
        warn(f"vision extraction failed for {key}: {e}; keeping OCR result")
        return None, False
    if fields is None:  # over the call cap: the run summary reports how many, once
        return None, True
    if not isinstance(fields, dict):
        warn(f"vision extraction failed for {key}: unexpected codex output: {type(fields).__name__}; "
             "keeping OCR result")
        return None, False
    cache.put(key, "codex", version, fields)
    return fields, False


def extract_image(
    path: Path, image_id: str, fmt: str, *, cache: ExtractionCache, codex: CodexRunner | None,
    force_vision: bool, ocr=recognize, warn=log, host_exists: Callable[[str], bool] | None = None,
) -> AdCopy:
    """Copy of a `simgad` image: OCR + layout rules, Codex when OCR fails or with `force_vision`.
    `host_exists` (optional, network) verifies the domains OCR read. Raises OcrUnavailable when neither
    OCR nor Codex could read the image, so the item fails and is retried on the next run."""
    is_text = str(fmt).lower() in ("text", "1")
    key = f"simgad:{image_id}"  # older caches keyed by the bare ID are simply not found: OCR runs again

    def load_ocr() -> OcrResult:
        for method in OCR_METHODS:
            if (hit := cache.get(key, method)) is not None:
                return _ocr_from_dict(hit)
        res = ocr(Path(path))
        cache.put(key, res.method, "", _ocr_to_dict(res))
        return res

    def parse(res: OcrResult) -> AdCopy | None:
        c = parse_text_ad_ocr(res) if is_text else parse_image_ad_ocr(res)
        if c is not None and host_exists is not None:
            verify_hosts(c, host_exists)
        return c

    r: OcrResult | None = None
    no_ocr: OcrUnavailable | None = None
    if not force_vision:
        try:
            r = load_ocr()
        except OcrUnavailable as e:
            no_ocr = e
    copy = parse(r) if r is not None else None
    pending = False
    if force_vision or no_ocr is not None or needs_vision(r, copy):
        fields, pending = _vision(path, key, cache, codex, warn)
        if fields is not None:
            return _vision_copy(fields, codex.model)
        if r is None and no_ocr is None:  # forced vision yielded nothing: fall back to OCR
            try:
                r = load_ocr()
                copy = parse(r)
            except OcrUnavailable as e:
                no_ocr = e
        if r is None:
            raise no_ocr

    if copy is None and r.lines:
        copy = parse_image_ad_ocr(r)  # best effort when the text layout did not match
        if is_text:
            copy.cta = ""  # a Text ad has no button; the last short line is a sitelink
        if host_exists is not None:
            verify_hosts(copy, host_exists)
    copy = copy or AdCopy()
    copy.vision_pending = pending
    return copy
