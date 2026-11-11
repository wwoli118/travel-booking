"""Duffel Stays 검색 어댑터 (조회 전용).

- 검색: POST /stays/search
- 요금 조회: POST /stays/search_results/{id}/actions/fetch_all_rates
- 견적(가격·가용성 재확인, 예약·결제 아님): POST /stays/quotes
- 예약(POST /stays/bookings)은 생성 즉시 Duffel 잔액에서 결제되므로 호출하지 않는다.
  src/duffel.py 차단 목록에도 들어 있다 (CLAUDE.md 원칙 1).
- 테스트 키로는 Duffel 테스트 호텔만 검색된다. 실제 도시·가격은 운영 키가 필요하다 (원칙 4: 별도 승인).

요청·응답 형식은 Duffel 공식 SDK(@duffel/api)의 Stays 타입 정의를 기준으로 했다.
"""

from __future__ import annotations

import sys
from decimal import Decimal

import httpx
from pydantic import ValidationError

from ..duffel import duffel_request
from ..hotel_models import (
    CancellationStep,
    HotelOffer,
    HotelProperty,
    HotelQuote,
    HotelRate,
    HotelSearchQuery,
)
from ..offers import parse_ts


def build_search_body(query: HotelSearchQuery) -> dict:
    data = {
        "location": {
            "radius": query.location.radius_km,
            "geographic_coordinates": {
                "latitude": query.location.latitude,
                "longitude": query.location.longitude,
            },
        },
        "check_in_date": query.check_in.isoformat(),
        "check_out_date": query.check_out.isoformat(),
        "rooms": query.rooms,
        "guests": [{"type": "adult"} for _ in range(query.adults)],
    }
    if query.free_cancellation_only:
        data["free_cancellation_only"] = True
    return {"data": data}


def offer_from_result(result: dict, nights: int) -> HotelOffer:
    acc = result["accommodation"]
    address = (acc.get("location") or {}).get("address") or {}
    addr_line = ", ".join(x for x in (address.get("line_one"), address.get("city_name"), address.get("country_code")) if x)
    return HotelOffer(
        provider="duffel",
        search_result_id=result["id"],
        accommodation_id=acc["id"],
        name=acc["name"],
        rating=acc.get("rating"),
        review_score=acc.get("review_score"),
        city=address.get("city_name"),
        address=addr_line or None,
        nights=nights,
        total_amount=result["cheapest_rate_total_amount"],
        total_currency=result["cheapest_rate_currency"],
        due_at_accommodation_amount=result.get("cheapest_rate_due_at_accommodation_amount"),
        due_at_accommodation_currency=result.get("cheapest_rate_due_at_accommodation_currency"),
        expires_at=parse_ts(result.get("expires_at")),
    )


class DuffelStaysProvider:
    name = "duffel"

    def search(
        self, query: HotelSearchQuery, api_key: str, client: httpx.Client | None = None
    ) -> list[HotelOffer]:
        body = duffel_request("POST", "/stays/search", api_key, client=client, json=build_search_body(query))
        offers: list[HotelOffer] = []
        for result in (body.get("data") or {}).get("results", []):
            try:
                offers.append(offer_from_result(result, query.nights))
            except (KeyError, TypeError, ValueError, ValidationError) as e:
                # 조용히 버리지 않고 어떤 결과가 왜 빠졌는지 알린다
                reason = f"필수 항목 없음: {e.args[0]}" if isinstance(e, KeyError) else str(e)
                print(f"[경고] 검색 결과 {result.get('id', '?')}를 해석하지 못해 제외합니다 ({reason})", file=sys.stderr)
        return offers


def _dec(v) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def parse_property(acc: dict) -> HotelProperty:
    address = (acc.get("location") or {}).get("address") or {}
    addr = ", ".join(x for x in (address.get("line_one"), address.get("city_name"), address.get("country_code")) if x)
    info = acc.get("check_in_information") or {}
    return HotelProperty(
        accommodation_id=acc["id"],
        name=acc["name"],
        rating=acc.get("rating"),
        review_score=acc.get("review_score"),
        address=addr or None,
        email=acc.get("email"),
        phone=acc.get("phone_number"),
        check_in_after=info.get("check_in_after_time"),
        check_out_before=info.get("check_out_before_time"),
    )


def parse_rate(rate: dict, room_name: str) -> HotelRate:
    return HotelRate(
        rate_id=rate["id"],
        room_name=room_name,
        board_type=rate.get("board_type"),
        payment_type=rate.get("payment_type"),
        total_amount=rate["total_amount"],
        total_currency=rate["total_currency"],
        due_at_accommodation_amount=_dec(rate.get("due_at_accommodation_amount")),
        due_at_accommodation_currency=rate.get("due_at_accommodation_currency") or None,
        cancellation=[
            CancellationStep(before=parse_ts(c["before"]), refund_amount=c["refund_amount"], currency=c["currency"])
            for c in rate.get("cancellation_timeline") or []
        ],
        conditions=[(c.get("title", ""), c.get("description", "")) for c in rate.get("conditions") or []],
        available_payment_methods=rate.get("available_payment_methods") or [],
        loyalty_programme_required=bool(rate.get("loyalty_programme_required", False)),
        expires_at=parse_ts(rate.get("expires_at")),
    )


def rates_from_accommodation(acc: dict) -> list[HotelRate]:
    rates: list[HotelRate] = []
    for room in acc.get("rooms") or []:
        for rate in room.get("rates") or []:
            try:
                rates.append(parse_rate(rate, room.get("name") or "객실 정보 없음"))
            except (KeyError, TypeError, ValueError, ValidationError) as e:
                reason = f"필수 항목 없음: {e.args[0]}" if isinstance(e, KeyError) else str(e)
                print(f"[경고] 요금 {rate.get('id', '?')}를 해석하지 못해 제외합니다 ({reason})", file=sys.stderr)
    return rates


def fetch_rates(
    search_result_id: str, api_key: str, client: httpx.Client | None = None
) -> tuple[HotelProperty, list[HotelRate]]:
    body = duffel_request(
        "POST", f"/stays/search_results/{search_result_id}/actions/fetch_all_rates", api_key, client=client
    )
    acc = body["data"]["accommodation"]
    return parse_property(acc), rates_from_accommodation(acc)


def create_quote(rate_id: str, api_key: str, client: httpx.Client | None = None) -> HotelQuote:
    """견적 생성: 가용성·총액 재확인. 예약·결제가 아니다."""
    data = duffel_request("POST", "/stays/quotes", api_key, client=client, json={"data": {"rate_id": rate_id}})["data"]
    acc = data["accommodation"]
    rates = rates_from_accommodation(acc)
    return HotelQuote(
        quote_id=data["id"],
        property=parse_property(acc),
        check_in=data["check_in_date"],
        check_out=data["check_out_date"],
        rooms=data["rooms"],
        total_amount=data["total_amount"],
        total_currency=data["total_currency"],
        due_at_accommodation_amount=_dec(data.get("due_at_accommodation_amount")),
        due_at_accommodation_currency=data.get("due_at_accommodation_currency") or None,
        deposit_amount=_dec(data.get("deposit_amount")),
        deposit_currency=data.get("deposit_currency") or None,
        rate=next((r for r in rates if r.rate_id == rate_id), rates[0] if rates else None),
    )
