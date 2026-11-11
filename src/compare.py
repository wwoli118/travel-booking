"""가격 비교·정렬 (항공·호텔 공용)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from .models import FlightOffer

T = TypeVar("T")


class MixedCurrencyError(ValueError):
    """서로 다른 통화의 금액을 환율 없이 비교하려 할 때."""


def sort_by_amount(
    items: Iterable[T],
    tiebreak: Callable[[T], Any] = lambda _: 0,
) -> list[T]:
    """total_amount 오름차순 정렬. 같은 금액이면 tiebreak 순.

    통화(total_currency)가 섞여 있으면 금액끼리 비교할 수 없으므로 정렬하지 않고 오류를 낸다.
    """
    items = list(items)
    currencies = {getattr(i, "total_currency") for i in items}
    if len(currencies) > 1:
        raise MixedCurrencyError(
            f"결과에 여러 통화가 섞여 있어 가격순 정렬을 할 수 없습니다: {sorted(currencies)}"
        )
    return sorted(items, key=lambda i: (getattr(i, "total_amount"), tiebreak(i)))


def sort_by_price(offers: Iterable[FlightOffer]) -> list[FlightOffer]:
    """항공: 총액 오름차순, 같은 가격이면 출발 시각이 이른 순."""
    return sort_by_amount(offers, tiebreak=lambda o: o.departing_at)
