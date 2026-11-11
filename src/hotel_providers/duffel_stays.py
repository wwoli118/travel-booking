"""Duffel Stays 검색 어댑터 (조회 전용).

- 검색: POST /stays/search
- 예약(POST /stays/bookings)은 생성 즉시 Duffel 잔액에서 결제되므로 호출하지 않는다.
  src/duffel.py 차단 목록에도 들어 있다 (CLAUDE.md 원칙 1).
- 테스트 키로는 Duffel 테스트 호텔만 검색된다. 실제 도시·가격은 운영 키가 필요하다 (원칙 4: 별도 승인).

요청·응답 형식은 Duffel 공식 SDK(@duffel/api)의 Stays 타입 정의를 기준으로 했다.
"""

from __future__ import annotations

import sys

import httpx
from pydantic import ValidationError

from ..duffel import duffel_request
from ..hotel_models import HotelOffer, HotelSearchQuery
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
