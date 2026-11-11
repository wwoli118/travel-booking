from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from src.order_status import OrderStatus, fetch_order_status


def order(**over):
    base = {
        "id": "ord_1", "booking_reference": "RZPVXK", "live_mode": False,
        "total_amount": "289.00", "total_currency": "USD", "cancelled_at": None,
        "payment_status": {"awaiting_payment": True, "payment_required_by": "2026-11-12T09:00:00Z", "paid_at": None},
        "documents": [],
    }
    base.update(over)
    return base


def test_awaiting():
    st = OrderStatus.from_duffel(order())
    assert st.state == "awaiting" and st.label == "결제대기"
    assert st.payment_required_by == datetime(2026, 11, 12, 9, tzinfo=timezone.utc)
    assert st.total_amount == Decimal("289.00")


def test_paid_with_tickets():
    st = OrderStatus.from_duffel(order(
        payment_status={"awaiting_payment": False, "paid_at": "2026-11-11T05:00:00Z"},
        documents=[{"type": "electronic_ticket", "unique_identifier": "1251234567890"},
                   {"type": "electronic_miscellaneous_document_associated", "unique_identifier": "999"}],
    ))
    assert st.state == "paid" and st.paid_at == datetime(2026, 11, 11, 5, tzinfo=timezone.utc)
    assert st.ticket_numbers == ["1251234567890"]


def test_cancelled_wins_over_payment_flag():
    st = OrderStatus.from_duffel(order(cancelled_at="2026-11-12T09:01:00Z"))
    assert st.state == "cancelled"


def test_missing_payment_status_is_not_assumed_paid():
    with pytest.raises(ValueError, match="결제 상태 정보가 없습니다"):
        OrderStatus.from_duffel(order(payment_status={}))


def test_deadline_passed():
    st = OrderStatus.from_duffel(order())
    assert st.deadline_passed(datetime(2026, 11, 12, 9, tzinfo=timezone.utc))
    assert not st.deadline_passed(datetime(2026, 11, 12, 8, 59, tzinfo=timezone.utc))
    paid = OrderStatus.from_duffel(order(payment_status={"awaiting_payment": False}))
    assert not paid.deadline_passed(datetime(2030, 1, 1, tzinfo=timezone.utc))


def test_fetch_uses_get_only():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"data": order()})

    client = httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(handler))
    assert fetch_order_status("ord_1", "duffel_test_x", client=client).order_id == "ord_1"
    assert seen == [("GET", "/air/orders/ord_1")]
