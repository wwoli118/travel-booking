"""Duffel 주문 상태 조회 (GET 전용). 결제 여부·취소 여부·항공권 번호를 확인한다.

이 모듈은 조회만 한다. 결제는 사람이 Duffel 대시보드에서 직접 한다 (CLAUDE.md 원칙 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from .duffel import duffel_request
from .offers import parse_ts

PAYMENT_PAID = "paid"
PAYMENT_AWAITING = "awaiting"
PAYMENT_CANCELLED = "cancelled"


@dataclass(frozen=True)
class OrderStatus:
    order_id: str
    booking_reference: str | None
    state: str  # paid / awaiting / cancelled
    paid_at: datetime | None
    payment_required_by: datetime | None
    cancelled_at: datetime | None
    total_amount: Decimal | None
    total_currency: str | None
    ticket_numbers: list[str] = field(default_factory=list)
    live_mode: bool = False

    def deadline_passed(self, now: datetime | None = None) -> bool:
        if self.state != PAYMENT_AWAITING or self.payment_required_by is None:
            return False
        return self.payment_required_by <= (now or datetime.now(timezone.utc))

    @property
    def label(self) -> str:
        return {PAYMENT_PAID: "결제완료", PAYMENT_AWAITING: "결제대기", PAYMENT_CANCELLED: "취소"}[self.state]

    @classmethod
    def from_duffel(cls, order: dict) -> "OrderStatus":
        ps = order.get("payment_status") or {}
        cancelled_at = parse_ts(order.get("cancelled_at"))
        if cancelled_at is not None:
            state = PAYMENT_CANCELLED
        elif ps.get("awaiting_payment") is False:
            state = PAYMENT_PAID
        elif ps.get("awaiting_payment") is True:
            state = PAYMENT_AWAITING
        else:
            # 판단 근거가 없으면 결제됐다고 가정하지 않는다
            raise ValueError(f"주문 {order.get('id')}의 결제 상태 정보가 없습니다 (payment_status.awaiting_payment).")
        amount = order.get("total_amount")
        tickets = [
            d["unique_identifier"]
            for d in order.get("documents") or []
            if d.get("type") == "electronic_ticket" and d.get("unique_identifier")
        ]
        return cls(
            order_id=order["id"],
            booking_reference=order.get("booking_reference"),
            state=state,
            paid_at=parse_ts(ps.get("paid_at")),
            payment_required_by=parse_ts(ps.get("payment_required_by")),
            cancelled_at=cancelled_at,
            total_amount=Decimal(amount) if amount is not None else None,
            total_currency=order.get("total_currency"),
            ticket_numbers=tickets,
            live_mode=bool(order.get("live_mode", False)),
        )


def fetch_order_status(order_id: str, api_key: str, client: httpx.Client | None = None) -> OrderStatus:
    body = duffel_request("GET", f"/air/orders/{order_id}", api_key, client=client)
    return OrderStatus.from_duffel(body["data"])
