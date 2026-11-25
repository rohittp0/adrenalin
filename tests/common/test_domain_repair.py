import pytest
from adrenalin.common.http import HostChecker
from adrenalin.tools.ads_transparency.copy import AdCopy, verify_hosts


@pytest.mark.parametrize("shown", ["www.flight-tare.com/flights", "WWW.FLIGHT-TARE.COM/flights"])
def test_repairs_known_domain_without_losing_path(monkeypatch, shown):
    checker = HostChecker(None)
    checker.remember('flight-fare.com')
    monkeypatch.setattr(checker, '_check', lambda h: h.removeprefix('www.') == 'flight-fare.com')
    copy = AdCopy(display_url=shown, website='flight-tare.com')
    verify_hosts(copy, checker)
    assert copy.website == 'flight-fare.com'
    assert copy.display_url == 'www.flight-fare.com/flights'


def test_ambiguous_and_valid_domains_are_not_corrected(monkeypatch):
    checker = HostChecker(None)
    checker.remember('flight-fare.com')
    checker.remember('flight-care.com')
    monkeypatch.setattr(checker, '_check', lambda h: h != 'flight-tare.com' and h != 'www.flight-tare.com')
    assert checker.resolve('flight-tare.com') == ''
    assert checker.resolve('flight-bare.com') == 'flight-bare.com'
