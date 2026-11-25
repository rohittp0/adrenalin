import pytest

from adrenalin.common.geo import geo_id_to_region, region_to_geo_id


def test_region_to_geo_id():
    assert region_to_geo_id("anywhere") is None
    assert region_to_geo_id("IN") == 2356 and region_to_geo_id("us") == 2840 and region_to_geo_id("ES") == 2724
    with pytest.raises(ValueError, match="XX"):
        region_to_geo_id("XX")


def test_geo_id_to_region_round_trips():
    assert geo_id_to_region(2840) == "US"
    assert geo_id_to_region(region_to_geo_id("DE")) == "DE"
    assert geo_id_to_region(2999) is None and geo_id_to_region(1999) is None and geo_id_to_region(3000) is None
