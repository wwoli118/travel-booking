import pytest

from src.compare import MixedCurrencyError, sort_by_price
from src.models import FlightOffer


def test_sorts_by_price_then_departure(raw_offers):
    offers = [FlightOffer.from_duffel(r) for r in raw_offers[:3]]
    ids = [o.offer_id for o in sort_by_price(offers)]
    assert ids == ["off_cheap_one_stop", "off_same_price_later", "off_expensive_direct"]


def test_mixed_currency_raises(raw_offers):
    a = FlightOffer.from_duffel(raw_offers[0])
    b = FlightOffer.from_duffel(dict(raw_offers[1], total_currency="KRW"))
    with pytest.raises(MixedCurrencyError, match="KRW"):
        sort_by_price([a, b])


def test_empty_list():
    assert sort_by_price([]) == []
