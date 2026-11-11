"""Duffel API 편도 항공권 검색 (조회 전용).

이 모듈은 검색(offer request)만 한다. 주문·결제 엔드포인트는 호출하지 않는다 (CLAUDE.md 원칙 1).

실행:
    python -m src.flights --from ICN --to MNL --date 2026-12-20 --adults 2
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import httpx
from pydantic import ValidationError

from .compare import MixedCurrencyError, sort_by_price
from .config import ConfigError, load_duffel_key
from .duffel import DuffelError, duffel_request
from .export import save_flights_csv
from .models import FlightOffer, SearchQuery


def build_offer_request_body(query: SearchQuery) -> dict:
    return {
        "data": {
            "slices": [
                {
                    "origin": query.origin,
                    "destination": query.destination,
                    "departure_date": query.departure_date.isoformat(),
                }
            ],
            "passengers": [{"type": "adult"} for _ in range(query.adults)],
            "cabin_class": query.cabin_class,
        }
    }


def search_flights(
    query: SearchQuery, api_key: str, client: httpx.Client | None = None
) -> list[FlightOffer]:
    """Duffel에 검색을 요청하고 결과를 FlightOffer 목록으로 돌려준다 (정렬 전)."""
    body = duffel_request(
        "POST",
        "/air/offer_requests",
        api_key,
        client=client,
        params={"return_offers": "true"},
        json=build_offer_request_body(query),
    )
    offers: list[FlightOffer] = []
    for raw in body.get("data", {}).get("offers", []):
        try:
            offers.append(FlightOffer.from_duffel(raw))
        except (KeyError, ValueError, ValidationError) as e:
            # 조용히 버리지 않고 어떤 offer가 왜 빠졌는지 알린다
            print(f"[경고] offer {raw.get('id', '?')}를 해석하지 못해 제외합니다: {e}", file=sys.stderr)
    return offers


def format_table(offers: list[FlightOffer]) -> str:
    header = f"{'#':>3}  {'항공사':<22} {'편명':<16} {'출발(현지)':<16} {'도착(현지)':<16} {'경유':<7} {'총액':>18}"
    lines = [header, "-" * len(header.encode("utf-8"))]
    for i, o in enumerate(offers, start=1):
        lines.append(
            f"{i:>3}  {o.airline[:22]:<22} {o.flight_numbers[:16]:<16} "
            f"{o.departing_at:%Y-%m-%d %H:%M} {o.arriving_at:%Y-%m-%d %H:%M} "
            f"{o.stops_label:<7} {o.price_label:>18}"
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Duffel 샌드박스 편도 항공권 검색 (가격순)")
    p.add_argument("--from", dest="origin", required=True, help="출발 공항 IATA 코드 (예: ICN)")
    p.add_argument("--to", dest="destination", required=True, help="도착 공항 IATA 코드 (예: MNL)")
    p.add_argument("--date", dest="departure_date", required=True, help="출발일 YYYY-MM-DD")
    p.add_argument("--adults", type=int, default=1, help="성인 인원 (1~9, 기본 1)")
    p.add_argument(
        "--cabin",
        dest="cabin_class",
        default="economy",
        choices=["economy", "premium_economy", "business", "first"],
    )
    p.add_argument("--limit", type=int, default=20, help="화면에 보여줄 최대 건수 (CSV는 전체 저장)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        query = SearchQuery(
            origin=args.origin,
            destination=args.destination,
            departure_date=args.departure_date,
            adults=args.adults,
            cabin_class=args.cabin_class,
        )
    except ValidationError as e:
        for err in e.errors():
            field = ".".join(str(x) for x in err["loc"]) or "입력"
            msg = err["msg"].removeprefix("Value error, ")
            print(f"[입력 오류] {field}: {msg}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"[입력 오류] {e}", file=sys.stderr)
        return 2

    try:
        api_key = load_duffel_key()
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    print(
        f"검색 중: {query.origin} → {query.destination}, {query.departure_date}, "
        f"성인 {query.adults}명, {query.cabin_class} (Duffel 테스트 환경)"
    )
    searched_at = datetime.now().astimezone()
    try:
        offers = search_flights(query, api_key)
        offers = sort_by_price(offers)
    except (DuffelError, MixedCurrencyError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1

    if not offers:
        print("조건에 맞는 항공편이 없습니다.")
        return 0

    shown = offers[: args.limit]
    print(format_table(shown))
    if len(offers) > len(shown):
        print(f"... 외 {len(offers) - len(shown)}건 (CSV에 전체 저장)")
    path = save_flights_csv(query, offers, searched_at)
    print(f"\n총 {len(offers)}건, 저장: {path}")
    print("※ 샌드박스 결과는 실제 운항·가격이 아닌 테스트 데이터입니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
