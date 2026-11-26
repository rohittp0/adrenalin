import time

import pytest

from adrenalin.tools.meta_ad_library.api import MetaOptions, scrape

PAGE_ID = "120740541030"  # MakeMyTrip


@pytest.mark.live
def test_live_makemytrip_in(tmp_path):
    t0 = time.monotonic()
    res = scrape(MetaOptions(PAGE_ID, region="IN", status="active", stage="list", out=tmp_path))
    print(f"\nlive run: {res.listed} Creatives listed in {time.monotonic() - t0:.0f}s, "
          f"listing_complete={res.listing_complete}, blocked_until={res.blocked_until}, errors={res.errors}, "
          f"out={tmp_path}")
    assert res.blocked_until is None, "Meta blocked the run; see run.log under the printed out path"
    assert res.listing_complete
    assert res.listed >= 100
    assert not res.errors
