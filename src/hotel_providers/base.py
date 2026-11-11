"""호텔 공급자 공통 인터페이스.

공급자마다 요청 형식이 달라서, 검색은 공통 모델(HotelSearchQuery → HotelOffer)로 맞춘다.
Booking.com·Agoda는 파트너 승인 후 어댑터를 구현한다 (CLAUDE.md: 승인 전까지는 설계만).
예약 생성·결제 메서드는 두지 않는다 (CLAUDE.md 원칙 1).
"""

from __future__ import annotations

from typing import Protocol

import httpx

from ..hotel_models import HotelOffer, HotelSearchQuery


class HotelProviderError(Exception):
    """공급자 호출 실패. 원인을 담는다."""


class ProviderNotAvailable(HotelProviderError):
    """아직 사용할 수 없는 공급자 (파트너 승인 전 등)."""


class HotelProvider(Protocol):
    name: str

    def search(
        self, query: HotelSearchQuery, api_key: str, client: httpx.Client | None = None
    ) -> list[HotelOffer]: ...


class _NotApprovedProvider:
    """파트너 승인 전 공급자. 호출하면 이유를 알려준다."""

    name = ""
    reason = ""

    def search(self, query, api_key, client=None):  # noqa: ARG002
        raise ProviderNotAvailable(self.reason)


class BookingDemandProvider(_NotApprovedProvider):
    name = "booking"
    reason = (
        "Booking.com Demand API는 Managed Affiliate Partner 계약과 담당 매니저의 승인이 필요하고, "
        "숙소 검색은 파일럿 파트너에게만 열려 있습니다. 승인 후 어댑터를 구현합니다 (docs/hotel_search_design.md)."
    )


class AgodaProvider(_NotApprovedProvider):
    name = "agoda"
    reason = (
        "Agoda 파트너 API는 제휴 파트너 계약과 IP 등록이 필요합니다. 승인 후 어댑터를 구현합니다 "
        "(docs/hotel_search_design.md)."
    )


def get_provider(name: str) -> HotelProvider:
    from .duffel_stays import DuffelStaysProvider

    providers: dict[str, type] = {
        "duffel": DuffelStaysProvider,
        "booking": BookingDemandProvider,
        "agoda": AgodaProvider,
    }
    if name not in providers:
        raise HotelProviderError(f"알 수 없는 공급자입니다: {name!r} (사용 가능: {', '.join(providers)})")
    return providers[name]()
