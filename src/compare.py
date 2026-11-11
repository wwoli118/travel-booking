"""가격 비교·정렬."""

from __future__ import annotations

from collections.abc import Iterable

from .models import FlightOffer


class MixedCurrencyError(ValueError):
    """서로 다른 통화의 금액을 환율 없이 비교하려 할 때."""


def sort_by_price(offers: Iterable[FlightOffer]) -> list[FlightOffer]:
    """총액 오름차순 정렬. 같은 가격이면 출발 시각이 이른 순.

    통화가 섞여 있으면 금액끼리 비교할 수 없으므로 정렬하지 않고 오류를 낸다.
    """
    offers = list(offers)
    currencies = {o.total_currency for o in offers}
    if len(currencies) > 1:
        raise MixedCurrencyError(
            f"결과에 여러 통화가 섞여 있어 가격순 정렬을 할 수 없습니다: {sorted(currencies)}"
        )
    return sorted(offers, key=lambda o: (o.total_amount, o.departing_at))
