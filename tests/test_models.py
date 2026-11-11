from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.models import FlightOffer, SearchQuery

FUTURE = date.today() + timedelta(days=30)


def test_query_normalizes_iata_codes():
    q = SearchQuery(origin=" icn ", destination="mnl", departure_date=FUTURE, adults=2)
    assert (q.origin, q.destination) == ("ICN", "MNL")


@pytest.mark.parametrize("code", ["IC", "ICNN", "1CN", "인천"])
def test_query_rejects_bad_iata(code):
    with pytest.raises(ValidationError):
        SearchQuery(origin=code, destination="MNL", departure_date=FUTURE, adults=1)


def test_query_rejects_past_date():
    with pytest.raises(ValidationError, match="지났습니다"):
        SearchQuery(origin="ICN", destination="MNL", departure_date=date.today() - timedelta(days=1), adults=1)


def test_query_accepts_today():
    SearchQuery(origin="ICN", destination="MNL", departure_date=date.today(), adults=1)


@pytest.mark.parametrize("adults", [0, -1, 10])
def test_query_rejects_bad_passenger_count(adults):
    with pytest.raises(ValidationError):
        SearchQuery(origin="ICN", destination="MNL", departure_date=FUTURE, adults=adults)


@pytest.mark.parametrize("adults", [1, 9])
def test_query_accepts_passenger_bounds(adults):
    assert SearchQuery(origin="ICN", destination="MNL", departure_date=FUTURE, adults=adults).adults == adults


def test_query_rejects_same_origin_destination():
    with pytest.raises(ValueError, match="같습니다"):
        SearchQuery(origin="ICN", destination="ICN", departure_date=FUTURE, adults=1)


def test_from_duffel_direct(raw_offers):
    o = FlightOffer.from_duffel(raw_offers[0])
    assert o.airline == "Duffel Airways"
    assert o.flight_numbers == "ZZ1001"
    assert o.stops == 0 and o.stops_label == "직항"
    assert o.total_amount == Decimal("512.40") and o.total_currency == "USD"
    assert o.price_label == "512.40 USD"
    assert o.departing_at == datetime(2026, 12, 20, 9, 10)


def test_from_duffel_one_stop_uses_first_departure_and_last_arrival(raw_offers):
    o = FlightOffer.from_duffel(raw_offers[1])
    assert o.stops == 1 and o.stops_label == "경유 1회"
    assert o.flight_numbers == "ZZ2002 / ZZ2003"
    assert (o.origin, o.destination) == ("ICN", "MNL")
    assert o.departing_at == datetime(2026, 12, 20, 7, 0)
    assert o.arriving_at == datetime(2026, 12, 20, 13, 15)


def test_from_duffel_overnight_arrival(raw_offers):
    o = FlightOffer.from_duffel(raw_offers[2])
    assert o.arriving_at.date() == date(2026, 12, 21)


def test_from_duffel_rejects_offer_without_slices(raw_offers):
    with pytest.raises(ValueError):
        FlightOffer.from_duffel(raw_offers[3])


def test_amount_keeps_decimal_precision(raw_offers):
    raw = dict(raw_offers[0], total_amount="0.10", total_currency="KRW")
    o = FlightOffer.from_duffel(raw)
    assert o.total_amount + Decimal("0.20") == Decimal("0.30")
