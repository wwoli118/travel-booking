import csv
import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from src.compare import sort_by_price
from src.config import ConfigError
from src.export import save_flights_csv
from src.flights import DuffelError, build_offer_request_body, search_flights
from src.models import SearchQuery

QUERY = SearchQuery(origin="ICN", destination="MNL", departure_date=date.today() + timedelta(days=30), adults=2)


def make_client(handler):
    return httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(handler))


def test_request_body_one_way_with_passengers():
    body = build_offer_request_body(QUERY)["data"]
    assert len(body["slices"]) == 1
    assert body["slices"][0]["origin"] == "ICN"
    assert body["passengers"] == [{"type": "adult"}, {"type": "adult"}]


def test_search_sends_headers_and_skips_broken_offer(duffel_response, capsys):
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["Authorization"]
        seen["version"] = request.headers["Duffel-Version"]
        seen["path"] = request.url.path
        seen["return_offers"] = request.url.params["return_offers"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=duffel_response)

    offers = search_flights(QUERY, "duffel_test_x", client=make_client(handler))
    assert seen["auth"] == "Bearer duffel_test_x"
    assert seen["version"] == "v2"
    assert seen["path"] == "/air/offer_requests"
    assert seen["return_offers"] == "true"
    assert len(offers) == 3  # 깨진 offer 1건 제외
    assert "off_broken_no_slices" in capsys.readouterr().err  # 제외 사유를 출력


def test_search_refuses_live_key_without_calling_api():
    def handler(request):
        raise AssertionError("운영 키로는 API를 호출하면 안 됩니다")

    with pytest.raises(ConfigError):
        search_flights(QUERY, "duffel_live_x", client=make_client(handler))


def test_api_error_message_is_reported():
    def handler(request):
        return httpx.Response(
            422,
            json={"errors": [{"title": "Invalid", "message": "origin is invalid", "code": "validation_error"}]},
        )

    with pytest.raises(DuffelError, match="422.*origin is invalid"):
        search_flights(QUERY, "duffel_test_x", client=make_client(handler))


def test_network_error_is_reported():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(DuffelError, match="연결하지 못했습니다"):
        search_flights(QUERY, "duffel_test_x", client=make_client(handler))


def test_csv_export(duffel_response, tmp_path):
    offers = sort_by_price(
        search_flights(QUERY, "duffel_test_x", client=make_client(lambda r: httpx.Response(201, json=duffel_response)))
    )
    path = save_flights_csv(QUERY, offers, datetime(2026, 11, 11, 15, 30, 0), data_dir=tmp_path)
    assert path.name == f"flights_ICN-MNL_{QUERY.departure_date.isoformat()}_20261111-153000.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert [r["rank"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["total_amount"] == "289.00" and rows[0]["total_currency"] == "USD"
    assert rows[0]["stops"] == "1"
    assert rows[0]["adults"] == "2"
