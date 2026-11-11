import json
from datetime import datetime, timezone

import httpx
import pytest

from src import orders, summary
from src.offers import OfferDetails, SelectedOffer
from src.travelers import TravelerFile

FIX = __import__("pathlib").Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def test_key(monkeypatch):
    monkeypatch.setenv("DUFFEL_API_KEY", "duffel_test_x")


@pytest.fixture
def hold_response():
    return json.loads((FIX / "duffel_order_hold.json").read_text(encoding="utf-8"))


@pytest.fixture
def draft_path(tmp_path, single_offer, travelers_raw):
    travelers_raw["travelers"][0]["passport"] = {"number": "M12345678", "expires_on": "2031-05-01", "issuing_country": "KR"}
    travelers_raw["travelers"][1]["passport"] = {"number": "M87654321", "expires_on": "2030-01-01", "issuing_country": "KR"}
    path = summary.save_draft(
        OfferDetails.from_duffel(single_offer),
        TravelerFile.model_validate(travelers_raw),
        SelectedOffer("off_cheap_one_stop"),
        datetime(2026, 11, 11, tzinfo=timezone.utc),
        tmp_path / "drafts",
    )
    return path


class Recorder:
    """요청을 기록하는 가짜 Duffel."""

    def __init__(self, offer, order=None):
        self.offer, self.order, self.calls = offer, order, []

    def __call__(self, req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        if req.method == "GET" and req.url.path.startswith("/air/offers/"):
            return httpx.Response(200, json={"data": self.offer})
        if req.method == "POST" and req.url.path == "/air/orders":
            return httpx.Response(201, json=self.order)
        if req.url.path == "/air/order_cancellations":
            return httpx.Response(201, json={"data": {"id": "ore_1", "order_id": body["data"]["order_id"],
                                                      "refund_amount": "0.00", "refund_currency": "USD",
                                                      "expires_at": "2099-01-01T00:00:00Z"}})
        if req.url.path == "/air/order_cancellations/ore_1/actions/confirm":
            return httpx.Response(200, json={"data": {"id": "ore_1", "confirmed_at": "2026-11-11T12:00:00Z"}})
        return httpx.Response(404, json={"errors": [{"title": "Not found", "message": req.url.path}]})

    @property
    def order_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/air/orders"]


def run(argv, rec, answers, tmp_path, capsys):
    it = iter(answers)
    client = httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(rec))
    code = orders.main(argv, input_fn=lambda _: next(it), client=client,
                       orders_dir=tmp_path / "orders", drafts_dir=tmp_path / "drafts")
    return code, capsys.readouterr()


# ---- 요청서

def test_body_is_hold_without_payments(draft_path):
    d = orders.load_draft(draft_path)
    body = orders.build_order_body(d.offer_id, d.passengers, include_documents=False)["data"]
    assert body["type"] == "hold" and "payments" not in body
    assert body["selected_offers"] == ["off_cheap_one_stop"]
    assert [p["id"] for p in body["passengers"]] == ["pas_001", "pas_002"]
    assert body["passengers"][0]["family_name"] == "HONG" and body["passengers"][0]["born_on"] == "1985-03-02"
    assert "identity_documents" not in body["passengers"][0]  # 요구되지 않으면 여권 정보 안 보냄


def test_body_includes_passport_when_required(draft_path):
    d = orders.load_draft(draft_path)
    doc = orders.build_order_body(d.offer_id, d.passengers, include_documents=True)["data"]["passengers"][0]["identity_documents"][0]
    assert doc == {"type": "passport", "unique_identifier": "M12345678", "issuing_country_code": "KR", "expires_on": "2031-05-01"}


def test_unconfirmed_draft_rejected(tmp_path):
    p = tmp_path / "draft_x.json"
    p.write_text(json.dumps({"status": "something_else"}), encoding="utf-8")
    with pytest.raises(orders.OrderPrepError, match="확인\\(yes\\)된"):
        orders.load_draft(p)


def test_no_drafts(tmp_path):
    with pytest.raises(orders.OrderPrepError, match="준비 내용이 없습니다"):
        orders.latest_draft(tmp_path)


# ---- prepare

def test_dry_run_never_posts_order(draft_path, single_offer, tmp_path, capsys):
    rec = Recorder(single_offer)
    code, out = run(["prepare"], rec, [], tmp_path, capsys)  # 최근 draft 자동 선택
    assert code == 0 and rec.order_posts == []
    assert "점검 모드" in out.out and "보류(hold) — 결제 없음" in out.out
    saved = list((tmp_path / "orders").glob("order_request_*.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text())["request"]["data"]["type"] == "hold"


def test_hold_yes_creates_order(draft_path, single_offer, hold_response, tmp_path, capsys):
    rec = Recorder(single_offer, hold_response)
    code, out = run(["prepare", "--draft", str(draft_path), "--hold"], rec, ["yes"], tmp_path, capsys)
    assert code == 0 and len(rec.order_posts) == 1
    sent = rec.order_posts[0][2]["data"]
    assert sent["type"] == "hold" and "payments" not in sent
    assert "RZPVXK" in out.out and "결제되지 않았습니다" in out.out
    assert "결제 없이 보류 주문만 생성" in out.out and "예약(주문)과 결제를 하지 않습니다" not in out.out
    rec_file = next((tmp_path / "orders").glob("order_RZPVXK_*.json"))
    assert json.loads(rec_file.read_text())["status"] == "held_awaiting_payment"


def test_hold_no_does_not_post(draft_path, single_offer, tmp_path, capsys):
    rec = Recorder(single_offer)
    code, out = run(["prepare", "--hold"], rec, ["no"], tmp_path, capsys)
    assert code == 0 and rec.order_posts == [] and "만들지 않았습니다" in out.out


def test_instant_only_offer_refused(draft_path, single_offer, tmp_path, capsys):
    single_offer["payment_requirements"]["requires_instant_payment"] = True
    rec = Recorder(single_offer)
    code, out = run(["prepare", "--hold"], rec, [], tmp_path, capsys)
    assert code == 1 and rec.order_posts == [] and "즉시 결제가 필요" in out.err


def test_missing_payment_requirements_treated_as_instant(draft_path, single_offer, tmp_path, capsys):
    del single_offer["payment_requirements"]
    rec = Recorder(single_offer)
    code, _ = run(["prepare", "--hold"], rec, [], tmp_path, capsys)
    assert code == 1 and rec.order_posts == []


def test_expired_offer_stops(draft_path, single_offer, tmp_path, capsys):
    single_offer["expires_at"] = "2020-01-01T00:00:00Z"
    rec = Recorder(single_offer)
    code, out = run(["prepare", "--hold"], rec, [], tmp_path, capsys)
    assert code == 1 and rec.order_posts == [] and "유효 시간이 지났습니다" in out.err


def test_price_increase_warned(draft_path, single_offer, hold_response, tmp_path, capsys):
    single_offer["total_amount"] = "320.00"
    rec = Recorder(single_offer, hold_response)
    code, out = run(["prepare", "--hold"], rec, ["no"], tmp_path, capsys)
    assert "31.00 USD 인상" in out.out and "2단계 확인 시보다 가격이 올랐" in out.out


def test_passenger_ids_changed(draft_path, single_offer, tmp_path, capsys):
    single_offer["passengers"][1]["id"] = "pas_999"
    rec = Recorder(single_offer)
    code, out = run(["prepare"], rec, [], tmp_path, capsys)
    assert code == 2 and "승객 구성" in out.err


def test_passport_required_but_missing(tmp_path, single_offer, travelers_raw, capsys):
    summary.save_draft(OfferDetails.from_duffel(single_offer), TravelerFile.model_validate(travelers_raw),
                       SelectedOffer("off_cheap_one_stop"), datetime(2026, 11, 11, tzinfo=timezone.utc), tmp_path / "drafts")
    single_offer["passenger_identity_documents_required"] = True
    rec = Recorder(single_offer)
    code, out = run(["prepare", "--hold"], rec, [], tmp_path, capsys)
    assert code == 2 and rec.order_posts == [] and "여권 정보가 필요" in out.err


def test_passport_required_sent(draft_path, single_offer, hold_response, tmp_path, capsys):
    single_offer["passenger_identity_documents_required"] = True
    rec = Recorder(single_offer, hold_response)
    code, out = run(["prepare", "--hold"], rec, ["yes"], tmp_path, capsys)
    assert code == 0
    assert rec.order_posts[0][2]["data"]["passengers"][1]["identity_documents"][0]["unique_identifier"] == "M87654321"
    assert "M87654321" not in out.out  # 화면에는 가려서 표시


def test_live_offer_refused(draft_path, single_offer, tmp_path, capsys):
    single_offer["live_mode"] = True
    rec = Recorder(single_offer)
    code, out = run(["prepare", "--hold"], rec, [], tmp_path, capsys)
    assert code == 2 and rec.order_posts == [] and "운영(live)" in out.err


# ---- cancel

def test_cancel_no_does_not_confirm(single_offer, tmp_path, capsys):
    rec = Recorder(single_offer)
    code, out = run(["cancel", "--order", "ord_0000test"], rec, ["no"], tmp_path, capsys)
    assert code == 0 and not any("confirm" in c[1] for c in rec.calls)
    assert "환불액: 0.00 USD" in out.out


def test_cancel_yes_confirms(single_offer, tmp_path, capsys):
    rec = Recorder(single_offer)
    code, out = run(["cancel", "--order", "ord_0000test"], rec, ["yes"], tmp_path, capsys)
    assert code == 0 and rec.calls[-1][1] == "/air/order_cancellations/ore_1/actions/confirm"
    assert "취소 완료" in out.out and list((tmp_path / "orders").glob("cancel_ord_0000test_*.json"))
