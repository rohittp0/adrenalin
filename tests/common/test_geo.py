import pytest

from adrenalin.common.geo import region_to_geo_id


def test_region_to_geo_id():
    assert region_to_geo_id("anywhere") is None
    assert region_to_geo_id("IN") == 2356 and region_to_geo_id("us") == 2840 and region_to_geo_id("ES") == 2724
    with pytest.raises(ValueError, match="XX"):
        region_to_geo_id("XX")
