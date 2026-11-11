import csv
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from src import hotels
from src.compare import MixedCurrencyError
from src.duffel import BlockedEndpointError, duffel_request
from src.hotel_models import (
    HotelOffer, HotelSearchQuery, Location, LocationError, load_locations, resolve_location,
)
from src.hotel_providers import ProviderNotAvailable, get_provider
from src.hotel_providers.duffel_stays import build_search_body, offer_from_result

FIX = Path(__file__).parent / "fixtures"
TODAY = date.today()
CIN = TODAY + timedelta(days=30)
LOC = Location(key="manila-makati", label="Makati", latitude=14.5547, longitude=121.0244, radius_km=2)


@pytest.fixture
def search_response():
    return json.loads((FIX / "duffel_stays_search.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def test_key(monkeypatch):
    monkeypatch.setenv("DUFFEL_API_KEY", "duffel_test_x")


def q(**over):
    base = dict(location=LOC, check_in=CIN, check_out=CIN + timedelta(days=3), rooms=1, adults=1)
    return HotelSearchQuery(**{**base, **over})


# ---- 검색 조건

def test_nights():
    assert q().nights == 3
    assert q(check_out=CIN + timedelta(days=1)).nights == 1


@pytest.mark.parametrize("over,msg", [
    ({"check_in": TODAY - timedelta(days=1), "check_out": TODAY + timedelta(days=1)}, "지났습니다"),
    ({"check_out": CIN}, "체크아웃"),
    ({"check_out": CIN - timedelta(days=1)}, "체크아웃"),
    ({"check_in": TODAY + timedelta(days=331), "check_out": TODAY + timedelta(days=332)}, "330일"),
    ({"check_out": CIN + timedelta(days=100)}, "99박"),
    ({"rooms": 2, "adults": 1}, "객실 수"),
    ({"rooms": 10, "adults": 10}, "less than or equal"),
])
def test_query_validation(over, msg):
    with pytest.raises(ValidationError, match=msg):
        q(**over)


def test_query_boundaries_ok():
    q(check_in=TODAY, check_out=TODAY + timedelta(days=1))
    q(check_in=TODAY + timedelta(days=330), check_out=TODAY + timedelta(days=331))
    q(check_out=CIN + timedelta(days=99))
    q(rooms=1, adults=2)


# ---- 위치

def test_locations_file_valid():
    locs = load_locations()
    assert {"manila-makati", "manila-bgc", "cebu-it-park", "duffel-test"} <= set(locs)
    assert all(1 <= l.radius_km <= 20 for l in locs.values())


def test_resolve_location_by_name_and_radius_override():
    loc = resolve_location("manila-bgc", radius_km=5)
    assert loc.key == "manila-bgc" and loc.radius_km == 5


def test_resolve_location_by_coordinates():
    loc = resolve_location(None, 10.3, 123.9)
    assert (loc.key, loc.latitude, loc.radius_km) == ("custom", 10.3, 3)


@pytest.mark.parametrize("args,msg", [
    (("seoul",), "등록되지 않은 지명"),
    (("manila-bgc", 1.0, 2.0), "함께 쓸 수 없습니다"),
    ((None, 1.0, None), "--lat와 --lng"),
    (("manila-bgc", None, None, 50), "1~20km"),
])
def test_resolve_location_errors(args, msg):
    with pytest.raises(LocationError, match=msg):
        resolve_location(*args)


def test_bad_coordinates_rejected():
    with pytest.raises(ValidationError):
        resolve_location(None, 95.0, 10.0)


# ---- Duffel 요청·응답

def test_search_body_shape():
    body = build_search_body(q(rooms=2, adults=3, free_cancellation_only=True))["data"]
    assert body["location"] == {"radius": 2, "geographic_coordinates": {"latitude": 14.5547, "longitude": 121.0244}}
    assert body["check_in_date"] == CIN.isoformat() and body["check_out_date"] == (CIN + timedelta(days=3)).isoformat()
    assert body["rooms"] == 2 and body["guests"] == [{"type": "adult"}] * 3
    assert body["free_cancellation_only"] is True
    assert "free_cancellation_only" not in build_search_body(q())["data"]


def test_offer_parsing(search_response):
    o = offer_from_result(search_response["data"]["results"][1], nights=3)
    assert (o.name, o.rating, o.review_score, o.city) == ("Budget Inn", 3, 7.2, "Makati")
    assert o.address == "5 Makati Ave, Makati, PH"
    assert o.total_amount == Decimal("300.00") and o.price_label == "300.00 USD"
    assert o.nightly_average == Decimal("100.00")
    assert o.due_label == "15.50 PHP"  # 현장 추가 요금은 통화가 달라도 따로 표시
    assert o.expires_at == datetime(2099, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("total,nights,avg", [("100.00", 3, "33.33"), ("200.00", 3, "66.67"), ("0.05", 2, "0.03")])
def test_nightly_average_rounding(total, nights, avg):
    o = HotelOffer(provider="duffel", search_result_id="s", accommodation_id="a", name="H",
                   nights=nights, total_amount=total, total_currency="USD")
    assert o.nightly_average == Decimal(avg)


def test_due_label_none_or_zero():
    base = dict(provider="duffel", search_result_id="s", accommodation_id="a", name="H", nights=1,
                total_amount="1", total_currency="USD")
    assert HotelOffer(**base).due_label == "-"
    assert HotelOffer(**base, due_at_accommodation_amount="0").due_label == "-"


def _client(handler):
    return httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(handler))


def test_provider_search_posts_and_skips_broken(search_response, capsys):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200, json=search_response)

    offers = get_provider("duffel").search(q(), "duffel_test_x", client=_client(handler))
    assert [(m, p) for m, p, _ in seen] == [("POST", "/stays/search")]
    assert len(offers) == 3
    assert "srr_broken" in capsys.readouterr().err


def test_sort_by_price_then_review_score(search_response):
    offers = [offer_from_result(r, 3) for r in search_response["data"]["results"][:3]]
    assert [o.accommodation_id for o in hotels.sort_hotels(offers)] == ["acc_city", "acc_budget", "acc_grand"]


def test_mixed_currency_not_sorted(search_response):
    rs = search_response["data"]["results"]
    rs[0]["cheapest_rate_currency"] = "PHP"
    with pytest.raises(MixedCurrencyError):
        hotels.sort_hotels([offer_from_result(r, 3) for r in rs[:3]])


@pytest.mark.parametrize("name", ["booking", "agoda"])
def test_unapproved_providers_explain(name):
    with pytest.raises(ProviderNotAvailable, match="승인"):
        get_provider(name).search(q(), "duffel_test_x")


@pytest.mark.parametrize("path", ["/stays/bookings", "/stays/bookings/bok_1/actions/cancel",
                                  "/stays/booking_payment_instructions"])
def test_stays_booking_endpoints_blocked(path):
    def handler(req):
        raise AssertionError("차단되어야 할 요청이 전송됨")
    with pytest.raises(BlockedEndpointError):
        duffel_request("POST", path, "duffel_test_x", client=_client(handler), json={"data": {}})


# ---- CLI

def run(argv, handler, tmp_path, capsys):
    code = hotels.main(argv, client=_client(handler), data_dir=tmp_path)
    return code, capsys.readouterr()


def base_argv(*extra):
    return ["--location", "manila-makati", "--check-in", CIN.isoformat(),
            "--check-out", (CIN + timedelta(days=3)).isoformat(), *extra]


def test_cli_search_saves_csv(search_response, tmp_path, capsys):
    code, out = run(base_argv(), lambda r: httpx.Response(200, json=search_response), tmp_path, capsys)
    assert code == 0
    assert out.out.index("City Business Hotel") < out.out.index("Budget Inn") < out.out.index("Grand Test Hotel")
    assert "100.00 USD" in out.out and "15.50 PHP" in out.out and "예약·결제는 하지 않습니다" in out.out
    rows = list(csv.DictReader(next(tmp_path.glob("hotels_manila-makati_*.csv")).open(encoding="utf-8-sig")))
    assert [r["rank"] for r in rows] == ["1", "2", "3"]
    r = rows[1]
    assert (r["hotel_name"], r["total_amount"], r["total_currency"], r["nightly_average"]) == ("Budget Inn", "300.00", "USD", "100.00")
    assert (r["due_at_accommodation_amount"], r["due_at_accommodation_currency"]) == ("15.50", "PHP")
    assert (r["nights"], r["rooms"], r["adults"]) == ("3", "1", "1")


def test_cli_one_room_per_person_default(search_response, tmp_path, capsys):
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content)["data"])
        return httpx.Response(200, json=search_response)

    run(base_argv("--adults", "3"), handler, tmp_path, capsys)
    assert sent["rooms"] == 3 and len(sent["guests"]) == 3
    run(base_argv("--adults", "2", "--rooms", "1"), handler, tmp_path, capsys)
    assert sent["rooms"] == 1 and len(sent["guests"]) == 2


def test_cli_no_results_hints_test_hotel(tmp_path, capsys):
    code, out = run(base_argv(), lambda r: httpx.Response(200, json={"data": {"results": []}}), tmp_path, capsys)
    assert code == 0 and "duffel-test" in out.out and not list(tmp_path.glob("*.csv"))


def test_cli_api_error_reported(tmp_path, capsys):
    err = {"errors": [{"title": "Forbidden", "message": "Stays is not enabled", "code": "unavailable"}]}
    code, out = run(base_argv(), lambda r: httpx.Response(403, json=err), tmp_path, capsys)
    assert code == 1 and "Stays is not enabled" in out.err


def test_cli_rejects_live_key(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DUFFEL_API_KEY", "duffel_live_x")
    code, out = run(base_argv(), lambda r: (_ for _ in ()).throw(AssertionError("호출 금지")), tmp_path, capsys)
    assert code == 2 and "테스트 키가 아닙니다" in out.err


@pytest.mark.parametrize("argv,msg", [
    (["--location", "seoul", "--check-in", CIN.isoformat(), "--check-out", (CIN + timedelta(days=1)).isoformat()], "등록되지 않은 지명"),
    (["--location", "manila-bgc", "--check-in", CIN.isoformat()], "--check-out"),
    (base_argv("--provider", "agoda"), None),
])
def test_cli_input_errors(argv, msg, tmp_path, capsys):
    code, out = run(argv, lambda r: (_ for _ in ()).throw(AssertionError("호출 금지")), tmp_path, capsys)
    assert code in (1, 2)
    if msg:
        assert msg in out.err


def test_cli_list_locations(tmp_path, capsys):
    code, out = run(["--list-locations"], lambda r: None, tmp_path, capsys)
    assert code == 0 and "manila-makati" in out.out and "duffel-test" in out.out
