"""선택한 운임(offer)을 다시 조회해 가격·규정·만료·수하물을 확인한다 (조회 전용)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import BaseModel

from .duffel import duffel_request
from .models import FareCondition, FlightOffer


class OfferSelectionError(Exception):
    """검색 결과 CSV에서 운임을 고르지 못했을 때."""


@dataclass(frozen=True)
class SelectedOffer:
    """검색 단계에서 고른 운임과 그때의 가격."""

    offer_id: str
    searched_amount: Decimal | None = None
    searched_currency: str | None = None


def select_from_csv(csv_path: Path, rank: int) -> SelectedOffer:
    if not csv_path.exists():
        raise OfferSelectionError(f"검색 결과 파일이 없습니다: {csv_path}")
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        if row.get("rank") == str(rank):
            return SelectedOffer(
                offer_id=row["offer_id"],
                searched_amount=Decimal(row["total_amount"]),
                searched_currency=row["total_currency"],
            )
    raise OfferSelectionError(f"{csv_path.name}에 순위 {rank}번이 없습니다 (1~{len(rows)}번 중 선택).")


class OfferDetails(BaseModel):
    """요약에 필요한 운임 상세."""

    flight: FlightOffer
    cabin_class: str | None
    connection_airports: list[str]
    passenger_ids: list[str]  # 주문 단계에서 탑승객과 짝지을 Duffel 승객 ID
    expires_at: datetime | None
    refund: FareCondition | None
    change: FareCondition | None
    baggage: str
    live_mode: bool
    requires_instant_payment: bool = True  # 정보가 없으면 보류 불가로 본다 (안전한 쪽)
    payment_required_by: datetime | None = None
    price_guarantee_expires_at: datetime | None = None
    identity_documents_required: bool = False

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or datetime.now(timezone.utc))

    @classmethod
    def from_duffel(cls, offer: dict) -> "OfferDetails":
        flight = FlightOffer.from_duffel(offer)
        segments = offer["slices"][0]["segments"]
        first_pax = (segments[0].get("passengers") or [{}])[0]
        conditions = offer.get("conditions") or {}
        payment = offer.get("payment_requirements") or {}
        return cls(
            flight=flight,
            cabin_class=first_pax.get("cabin_class"),
            connection_airports=[s["destination"]["iata_code"] for s in segments[:-1]],
            passenger_ids=[p["id"] for p in offer.get("passengers", [])],
            expires_at=parse_ts(offer.get("expires_at")),
            refund=FareCondition.from_duffel(conditions.get("refund_before_departure")),
            change=FareCondition.from_duffel(conditions.get("change_before_departure")),
            baggage=summarize_baggage(segments),
            live_mode=bool(offer.get("live_mode", False)),
            requires_instant_payment=bool(payment.get("requires_instant_payment", True)),
            payment_required_by=parse_ts(payment.get("payment_required_by")),
            price_guarantee_expires_at=parse_ts(payment.get("price_guarantee_expires_at")),
            identity_documents_required=bool(offer.get("passenger_identity_documents_required", False)),
        )


def parse_ts(value: str | None) -> datetime | None:
    """Duffel ISO 시각(끝의 Z 포함)을 timezone 있는 datetime으로."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def summarize_baggage(segments: list[dict]) -> str:
    """구간·승객별 수하물을 한 줄로. 서로 다르면 다르다고 표시한다."""
    combos = set()
    for seg in segments:
        for pax in seg.get("passengers") or []:
            counts = {"checked": 0, "carry_on": 0}
            for b in pax.get("baggages") or []:
                if b.get("type") in counts:
                    counts[b["type"]] += int(b.get("quantity", 0))
            combos.add((counts["checked"], counts["carry_on"]))
    if not combos:
        return "정보 없음 (항공사 규정을 직접 확인하세요)"
    if len(combos) > 1:
        return "구간·승객별로 다름 (항공사 규정을 직접 확인하세요)"
    checked, carry_on = combos.pop()
    return f"위탁 {checked}개 / 기내 {carry_on}개 (1인당)"


def fetch_offer(offer_id: str, api_key: str, client: httpx.Client | None = None) -> OfferDetails:
    """GET /air/offers/{id} 로 최신 운임을 받아온다."""
    body = duffel_request("GET", f"/air/offers/{offer_id}", api_key, client=client)
    return OfferDetails.from_duffel(body["data"])
