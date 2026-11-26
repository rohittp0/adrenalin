"""Parsers for the Meta Ad Library's internal GraphQL responses.

Pure functions: no network, no queue. A Creative is one Library ad
(`ad_archive_id`). Its Variations are the DCO cards, or the single top-level
creative; CAROUSEL and DPA keep their Cards inside that one Variation.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

_NOT_FOUND = re.compile(r"GraphQL document with ID .* was not found")
_TEMPLATE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_CARD_FORMATS = {"CAROUSEL", "DPA"}


class GraphQLError(ValueError):
    pass


class DocNotFound(GraphQLError):
    pass


@dataclass(frozen=True)
class Card:
    title: str
    body: str
    link_url: str
    link_description: str
    image_url: str
    video_url: str


@dataclass(frozen=True)
class Variation:
    body: str
    title: str
    link_description: str
    caption: str
    cta_text: str
    link_url: str
    image_urls: tuple[str, ...]
    video_urls: tuple[str, ...]
    video_preview_urls: tuple[str, ...]
    cards: tuple[Card, ...]


@dataclass(frozen=True)
class MetaCreative:
    library_id: str
    page_id: str
    page_name: str
    display_format: str
    is_active: bool
    start_date: date
    end_date: date | None
    platforms: tuple[str, ...]
    collation_id: str | None
    collation_count: int | None
    variations: tuple[Variation, ...]


@dataclass(frozen=True)
class SearchPage:
    creatives: list[MetaCreative]
    next_cursor: str | None
    skipped: int


@dataclass(frozen=True)
class EuReach:
    eu_total: int | None
    eu_by_country: dict[str, int]
    uk_total: int | None
    payer: str
    beneficiary: str


def _text(value: Any) -> str:
    """A text field; Template copy (`{{ ... }}` placeholders) becomes ""."""
    if not isinstance(value, str) or _TEMPLATE.search(value):
        return ""
    return value


def _first(*values: Any) -> str:
    for value in values:
        if text := _text(value):
            return text
    return ""


def _date(seconds: Any) -> date | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(int(seconds), UTC).date()


def _urls(items: Any, *keys: str) -> tuple[str, ...]:
    out = []
    for item in items or []:
        if url := _first(*(item.get(k) for k in keys)):
            out.append(url)
    return tuple(out)


def _card(raw: dict) -> Card:
    return Card(
        title=_text(raw.get("title")),
        body=_text(raw.get("body")),
        link_url=_text(raw.get("link_url")),
        link_description=_text(raw.get("link_description")),
        image_url=_first(raw.get("original_image_url"), raw.get("resized_image_url")),
        video_url=_first(raw.get("video_hd_url"), raw.get("video_sd_url")),
    )


def _dco_variation(raw: dict) -> Variation:
    card = _card(raw)
    return Variation(
        body=card.body,
        title=card.title,
        link_description=card.link_description,
        caption=_text(raw.get("caption")),
        cta_text=_text(raw.get("cta_text")),
        link_url=card.link_url,
        image_urls=(card.image_url,) if card.image_url else (),
        video_urls=(card.video_url,) if card.video_url else (),
        video_preview_urls=_urls([raw], "video_preview_image_url"),
        cards=(),
    )


def _top_level_variation(snapshot: dict, cards: tuple[Card, ...]) -> Variation:
    body = snapshot.get("body")
    videos = snapshot.get("videos")
    return Variation(
        body=_text(body.get("text") if isinstance(body, dict) else None),
        title=_text(snapshot.get("title")),
        link_description=_text(snapshot.get("link_description")),
        caption=_text(snapshot.get("caption")),
        cta_text=_text(snapshot.get("cta_text")),
        link_url=_text(snapshot.get("link_url")),
        image_urls=_urls(snapshot.get("images"), "original_image_url", "resized_image_url"),
        video_urls=_urls(videos, "video_hd_url", "video_sd_url"),
        video_preview_urls=_urls(videos, "video_preview_image_url"),
        cards=cards,
    )


def _variations(snapshot: dict) -> tuple[Variation, ...]:
    fmt = snapshot.get("display_format")
    raw_cards = [c for c in snapshot.get("cards") or [] if isinstance(c, dict)]
    if raw_cards and fmt == "DCO":
        return tuple(_dco_variation(c) for c in raw_cards)
    if raw_cards and fmt in _CARD_FORMATS:
        return (_top_level_variation(snapshot, tuple(_card(c) for c in raw_cards)),)
    return (_top_level_variation(snapshot, ()),)


def _creative(raw: dict) -> MetaCreative:
    snapshot = raw["snapshot"]
    start = _date(raw["start_date"])
    if start is None:
        raise ValueError("no start_date")
    count = raw.get("collation_count")
    collation_id = raw.get("collation_id")
    return MetaCreative(
        library_id=str(raw["ad_archive_id"]),
        page_id=str(raw["page_id"]),
        page_name=raw.get("page_name") or snapshot.get("page_name") or "",
        display_format=snapshot["display_format"],
        is_active=bool(raw["is_active"]),
        start_date=start,
        end_date=_date(raw.get("end_date")),
        platforms=tuple(raw.get("publisher_platform") or ()),
        collation_id=str(collation_id) if collation_id else None,
        collation_count=int(count) if count is not None else None,
        variations=_variations(snapshot),
    )


def check_errors(body: str) -> None:
    """Raise `DocNotFound` or `GraphQLError` when the response is an error."""
    data = json.loads(body)
    errors = data.get("errors")
    if not errors or data.get("data"):
        return
    message = "; ".join(str(e.get("message", e)) for e in errors if e) or "unknown error"
    if _NOT_FOUND.search(message):
        raise DocNotFound(message)
    raise GraphQLError(message)


def parse_search_page(body: str) -> SearchPage:
    check_errors(body)
    connection = json.loads(body)["data"]["ad_library_main"]["search_results_connection"]
    creatives: list[MetaCreative] = []
    skipped = 0
    for edge in connection.get("edges") or []:
        for raw in ((edge or {}).get("node") or {}).get("collated_results") or []:
            try:
                creatives.append(_creative(raw))
            except (KeyError, TypeError, ValueError, AttributeError):
                skipped += 1
    info = connection.get("page_info") or {}
    cursor = info.get("end_cursor") if info.get("has_next_page") else None
    return SearchPage(creatives=creatives, next_cursor=cursor or None, skipped=skipped)


def _reach(value: Any) -> int:
    return value if isinstance(value, int) else 0


def parse_detail(body: str) -> EuReach | None:
    """EU and UK reach plus payer/beneficiary; None when neither region has data."""
    check_errors(body)
    details = json.loads(body)["data"]["ad_library_main"]["ad_details"] or {}
    by_location = details.get("transparency_by_location") or {}
    eu = by_location.get("eu_transparency")
    uk = by_location.get("uk_transparency")
    if not eu and not uk:
        return None
    by_country: dict[str, int] = {}
    for entry in (eu or {}).get("age_country_gender_reach_breakdown") or []:
        total = 0
        for row in entry.get("age_gender_breakdowns") or []:
            total += sum(_reach(row.get(k)) for k in ("male", "female", "unknown"))
        by_country[entry["country"]] = by_country.get(entry["country"], 0) + total
    people = (details.get("aaa_info") or {}).get("payer_beneficiary_data") or []
    first = people[0] if people else {}
    return EuReach(
        eu_total=(eu or {}).get("eu_total_reach"),
        eu_by_country=by_country,
        uk_total=(uk or {}).get("total_reach"),
        payer=first.get("payer") or "",
        beneficiary=first.get("beneficiary") or "",
    )
