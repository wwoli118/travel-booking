from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from src.models import FareCondition
from src.offers import (
    OfferDetails,
    OfferSelectionError,
    fetch_offer,
    select_from_csv,
    summarize_baggage,
)


def test_details_parsed(single_offer):
    d = OfferDetails.from_duffel(single_offer)
    assert d.passenger_ids == ["pas_001", "pas_002"]
    assert d.connection_airports == ["TPE"]
    assert d.cabin_class == "economy"
    assert d.baggage == "위탁 1개 / 기내 1개 (1인당)"
    assert d.expires_at == datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert d.refund.allowed and d.refund.penalty_amount == Decimal("50.00")
    assert not d.change.allowed


def test_expiry():
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    raw = {"expires_at": "2026-11-11T12:00:00Z"}
    d = OfferDetails.model_construct(expires_at=datetime.fromisoformat(raw["expires_at"].replace("Z", "+00:00")))
    assert d.is_expired(now)  # 정각이면 만료
    assert not OfferDetails.model_construct(expires_at=None).is_expired(now)


@pytest.mark.parametrize("raw,label", [
    (None, "정보 없음 (항공사 규정을 직접 확인하세요)"),
    ({"allowed": False}, "불가"),
    ({"allowed": True, "penalty_amount": None}, "가능 (수수료 정보 없음)"),
    ({"allowed": True, "penalty_amount": "0.00", "penalty_currency": "USD"}, "가능, 수수료 없음"),
    ({"allowed": True, "penalty_amount": "1500.5", "penalty_currency": "PHP"}, "가능, 수수료 1,500.50 PHP"),
])
def test_condition_labels(raw, label):
    assert FareCondition.label(FareCondition.from_duffel(raw)) == label


def test_baggage_differs_or_missing(single_offer):
    segs = single_offer["slices"][0]["segments"]
    segs[1]["passengers"][0]["baggages"] = []
    assert "다름" in summarize_baggage(segs)
    assert "정보 없음" in summarize_baggage([{"passengers": []}])


def test_select_from_csv(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text("﻿rank,offer_id,total_amount,total_currency\n1,off_a,289.00,USD\n2,off_b,300.00,USD\n", encoding="utf-8")
    s = select_from_csv(p, 2)
    assert (s.offer_id, s.searched_amount, s.searched_currency) == ("off_b", Decimal("300.00"), "USD")
    with pytest.raises(OfferSelectionError, match="3번이 없습니다"):
        select_from_csv(p, 3)


def test_fetch_offer_uses_get(single_offer):
    seen = {}

    def handler(req):
        seen["method"], seen["path"] = req.method, req.url.path
        return httpx.Response(200, json={"data": single_offer})

    client = httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(handler))
    d = fetch_offer("off_cheap_one_stop", "duffel_test_x", client=client)
    assert seen == {"method": "GET", "path": "/air/offers/off_cheap_one_stop"}
    assert d.flight.offer_id == "off_cheap_one_stop"
