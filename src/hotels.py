"""호텔 검색·비교 (조회 전용, 1인 1실 기본).

숙소별 최저 요금을 총액 순으로 보여주고 data/에 CSV로 저장한다.
요금별 취소 규정·식사 포함 여부 확인과 예약 전 요약은 다음 단계에서 한다.
예약·결제는 하지 않는다 (CLAUDE.md 원칙 1).

실행:
    python -m src.hotels --location manila-makati --check-in 2026-12-20 --check-out 2026-12-23
    python -m src.hotels --location manila-bgc --check-in 2026-12-20 --check-out 2026-12-23 --adults 2 --free-cancel
    python -m src.hotels --lat 14.55 --lng 121.02 --radius 3 --check-in 2026-12-20 --check-out 2026-12-23
    python -m src.hotels --list-locations
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .compare import MixedCurrencyError, sort_by_amount
from .config import DATA_DIR, ConfigError, load_duffel_key
from .duffel import BlockedEndpointError, DuffelError
from .hotel_models import HotelOffer, HotelSearchQuery, LocationError, load_locations, resolve_location
from .hotel_providers import HotelProviderError, get_provider

CSV_COLUMNS = [
    "searched_at", "provider", "location", "check_in", "check_out", "nights", "rooms", "adults",
    "free_cancellation_only", "rank", "hotel_name", "rating", "review_score", "city", "address",
    "total_amount", "total_currency", "nightly_average", "due_at_accommodation_amount",
    "due_at_accommodation_currency", "search_result_id", "accommodation_id", "expires_at",
]


def sort_hotels(offers: list[HotelOffer]) -> list[HotelOffer]:
    """총액 오름차순. 같은 금액이면 리뷰 점수 높은 순, 그다음 이름 순. 통화가 섞이면 오류."""
    return sort_by_amount(offers, tiebreak=lambda o: (-(o.review_score or 0), o.name))


def save_hotels_csv(
    query: HotelSearchQuery, offers: list[HotelOffer], searched_at: datetime, data_dir: Path = DATA_DIR
) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    loc = re.sub(r"[^A-Za-z0-9_-]", "_", query.location.key)
    path = data_dir / f"hotels_{loc}_{query.check_in.isoformat()}_{searched_at:%Y%m%d-%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for rank, o in enumerate(offers, start=1):
            w.writerow({
                "searched_at": searched_at.isoformat(timespec="seconds"),
                "provider": o.provider,
                "location": query.location.label,
                "check_in": query.check_in.isoformat(),
                "check_out": query.check_out.isoformat(),
                "nights": query.nights,
                "rooms": query.rooms,
                "adults": query.adults,
                "free_cancellation_only": "Y" if query.free_cancellation_only else "N",
                "rank": rank,
                "hotel_name": o.name,
                "rating": o.rating if o.rating is not None else "",
                "review_score": o.review_score if o.review_score is not None else "",
                "city": o.city or "",
                "address": o.address or "",
                "total_amount": f"{o.total_amount:.2f}",
                "total_currency": o.total_currency,
                "nightly_average": f"{o.nightly_average:.2f}",
                "due_at_accommodation_amount": (
                    f"{o.due_at_accommodation_amount:.2f}" if o.due_at_accommodation_amount is not None else ""
                ),
                "due_at_accommodation_currency": o.due_at_accommodation_currency or "",
                "search_result_id": o.search_result_id,
                "accommodation_id": o.accommodation_id,
                "expires_at": o.expires_at.isoformat() if o.expires_at else "",
            })
    return path


def format_table(offers: list[HotelOffer]) -> str:
    lines = [f"{'#':>3}  {'숙소':<34} {'성급':<4} {'평점':<5} {'총액':>18} {'1박 평균':>16} {'현장 추가':>14}"]
    for i, o in enumerate(offers, start=1):
        stars = f"{o.rating}★" if o.rating else "-"
        score = f"{o.review_score:.1f}" if o.review_score is not None else "-"
        lines.append(
            f"{i:>3}  {o.name[:34]:<34} {stars:<4} {score:<5} {o.price_label:>18} {o.nightly_label:>16} {o.due_label:>14}"
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="호텔 검색·비교 (조회 전용, 예약·결제 없음)")
    p.add_argument("--list-locations", action="store_true", help="등록된 출장지 목록 보기")
    p.add_argument("--location", help="등록 지명 (예: manila-makati). --list-locations로 확인")
    p.add_argument("--lat", type=float, help="위도 (등록 지명 대신)")
    p.add_argument("--lng", type=float, help="경도 (등록 지명 대신)")
    p.add_argument("--radius", type=int, help="검색 반경 km (1~20, 기본: 지명별 값 또는 3)")
    p.add_argument("--check-in", help="체크인 YYYY-MM-DD")
    p.add_argument("--check-out", help="체크아웃 YYYY-MM-DD")
    p.add_argument("--adults", type=int, help="총 인원 (기본 1)")
    p.add_argument("--rooms", type=int, help="객실 수 (기본: 인원과 같게, 1인 1실)")
    p.add_argument("--free-cancel", action="store_true", help="무료 취소 가능한 요금만")
    p.add_argument("--provider", default="duffel", help="duffel (기본) / booking / agoda (승인 후)")
    p.add_argument("--limit", type=int, default=20, help="화면 표시 건수 (CSV는 전체)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, client=None, data_dir: Path = DATA_DIR) -> int:
    args = parse_args(argv)
    if args.list_locations:
        for key, loc in load_locations().items():
            print(f"{key:<16} {loc.label} ({loc.latitude}, {loc.longitude}, 반경 {loc.radius_km}km)")
        return 0
    if not args.check_in or not args.check_out:
        print("[입력 오류] --check-in과 --check-out을 지정하세요.", file=sys.stderr)
        return 2

    adults = args.adults or args.rooms or 1
    rooms = args.rooms or adults  # 1인 1실 기본
    try:
        location = resolve_location(args.location, args.lat, args.lng, args.radius)
        query = HotelSearchQuery(
            location=location, check_in=args.check_in, check_out=args.check_out,
            rooms=rooms, adults=adults, free_cancellation_only=args.free_cancel,
        )
        provider = get_provider(args.provider)
        api_key = load_duffel_key()
    except ValidationError as e:
        for err in e.errors():
            field = ".".join(str(x) for x in err["loc"]) or "입력"
            print(f"[입력 오류] {field}: {err['msg'].removeprefix('Value error, ')}", file=sys.stderr)
        return 2
    except (LocationError, HotelProviderError, ConfigError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 2

    print(
        f"검색 중: {location.label} 반경 {location.radius_km}km, {query.check_in} ~ {query.check_out} "
        f"({query.nights}박), {query.rooms}실 {query.adults}명"
        + (", 무료 취소만" if query.free_cancellation_only else "")
        + f" ({provider.name}{', 테스트 환경' if provider.name == 'duffel' else ''})"
    )
    searched_at = datetime.now(timezone.utc).astimezone()
    try:
        offers = sort_hotels(provider.search(query, api_key, client=client))
    except (HotelProviderError, DuffelError, BlockedEndpointError, MixedCurrencyError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1

    if not offers:
        print("조건에 맞는 숙소가 없습니다.")
        if location.key != "duffel-test":
            print("※ Duffel 테스트 키로는 테스트 호텔만 검색됩니다. --location duffel-test 로 확인해 보세요.")
        return 0

    shown = offers[: args.limit]
    print(format_table(shown))
    if len(offers) > len(shown):
        print(f"... 외 {len(offers) - len(shown)}건 (CSV에 전체 저장)")
    path = save_hotels_csv(query, offers, searched_at, data_dir)
    print(f"\n총 {len(offers)}건, 저장: {path}")
    print("※ 숙소별 최저 요금입니다. '현장 추가'는 총액에 포함되지 않고 숙소에서 따로 내는 요금입니다.")
    print("※ 요금별 취소 규정·식사 포함 여부는 다음 단계(예약 전 확인)에서 확인합니다.")
    print("※ 테스트 데이터일 수 있습니다. 예약·결제는 하지 않습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
