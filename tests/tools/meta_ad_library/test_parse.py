import json
from datetime import date
from pathlib import Path

import pytest

from adrenalin.tools.meta_ad_library.parse import (
    DocNotFound,
    GraphQLError,
    check_errors,
    parse_detail,
    parse_search_page,
)

FIX = Path(__file__).parents[2] / "fixtures" / "meta_ad_library"


def load(name: str) -> str:
    return (FIX / name).read_text()


def test_first_page():
    page = parse_search_page(load("first_page.json"))
    assert len(page.creatives) == 30
    assert page.next_cursor is not None
    assert page.skipped == 0


def test_inactive_page():
    page = parse_search_page(load("inactive_page.json"))
    assert all(not c.is_active for c in page.creatives)
    carousels = [c for c in page.creatives if c.display_format == "CAROUSEL"]
    assert carousels
    assert any(len(c.variations) == 1 and len(c.variations[0].cards) >= 2 for c in carousels)
    for c in page.creatives:
        if c.display_format in ("CAROUSEL", "DPA"):
            assert len(c.variations) == 1


def test_dco_cards_are_variations():
    page = parse_search_page(load("search_page.json"))
    c = next(c for c in page.creatives if c.library_id == "1535930091652737")
    assert c.display_format == "DCO"
    assert len(c.variations) == 2
    for v in c.variations:
        assert v.title == "Get up to Rs 1500 off* on flight tickets- FLYAPP"
        assert v.cards == ()
        assert v.link_url == "http://play.google.com/store/apps/details?id=com.makemytrip"
        assert v.image_urls


def test_creative_fields():
    page = parse_search_page(load("search_page.json"))
    c = next(c for c in page.creatives if c.library_id == "1535930091652737")
    assert c.page_id == "120740541030"
    assert c.page_name == "MakeMyTrip"
    assert c.is_active is True
    assert c.start_date == date(2026, 5, 22)
    assert c.end_date == date(2026, 11, 26)
    assert c.platforms == ("FACEBOOK", "INSTAGRAM", "MESSENGER", "THREADS")
    assert c.collation_id == "2089002294967709"
    assert c.collation_count == 1


def test_image_and_video_creatives():
    seen = set()
    for name in ("first_page.json", "inactive_page.json", "search_page.json"):
        for c in parse_search_page(load(name)).creatives:
            if c.display_format == "IMAGE":
                assert len(c.variations) == 1
                assert c.variations[0].image_urls and c.variations[0].cards == ()
                seen.add("IMAGE")
            if c.display_format == "VIDEO":
                v = c.variations[0]
                assert v.video_urls and v.cards == ()
                seen.add("VIDEO")
    assert seen == {"IMAGE", "VIDEO"}


def test_template_copy_dropped():
    n = 0
    for name in ("first_page.json", "inactive_page.json", "search_page.json"):
        for c in parse_search_page(load(name)).creatives:
            for v in c.variations:
                n += 1
                texts = [v.body, v.title, v.link_description, v.caption, v.cta_text, v.link_url]
                for card in v.cards:
                    texts += [card.title, card.body, card.link_url, card.link_description]
                assert not any("{{" in t for t in texts)
    assert n > 0


def test_search_page_cursor():
    page = parse_search_page(load("search_page.json"))
    assert len(page.creatives) == 10
    assert page.next_cursor == "AQHTywakAyxKqjk5cAC3_hIKC1IxXkm3-ry0uvHMTLkOK6LNSeKu7kZBKGf1uYeoVSYh"


def test_malformed_result_is_skipped():
    body = json.loads(load("search_page.json"))
    edges = body["data"]["ad_library_main"]["search_results_connection"]["edges"]
    edges[0]["node"]["collated_results"].append({"ad_archive_id": "9"})
    page = parse_search_page(json.dumps(body))
    assert len(page.creatives) == 10
    assert page.skipped == 1


def test_detail_eu():
    eu = parse_detail(load("detail_eu.json"))
    assert eu is not None
    assert eu.eu_total == 22
    assert eu.eu_by_country["DE"] == 3
    assert eu.uk_total == 4
    assert (eu.payer, eu.beneficiary) == ("", "")


def test_detail_no_eu():
    assert parse_detail(load("detail_no_eu.json")) is None


def test_doc_not_found_is_distinct():
    with pytest.raises(DocNotFound):
        check_errors(load("doc_not_found.json"))
    with pytest.raises(GraphQLError) as e:
        check_errors('{"errors":[{"message":"x"}]}')
    assert not isinstance(e.value, DocNotFound)
    check_errors(load("search_page.json"))
